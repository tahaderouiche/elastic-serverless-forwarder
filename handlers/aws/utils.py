# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.

import hashlib
import json
import os
from typing import Any, Callable, Optional

import boto3
from aws_lambda_typing import context as context_
from botocore.client import BaseClient as BotoBaseClient
from elasticapm import Client, get_client
from elasticapm.contrib.serverless.aws import capture_serverless as apm_capture_serverless  # noqa: F401

from share import Config, Input, Output, shared_logger
from shippers import CompositeShipper, ElasticsearchShipper, ShipperFactory
from storage import CommonStorage, StorageFactory

_available_triggers: dict[str, str] = {"aws:sqs": "s3-sqs", "aws:kinesis": "kinesis-data-stream"}

CONFIG_FROM_PAYLOAD: str = "CONFIG_FROM_PAYLOAD"
CONFIG_FROM_S3FILE: str = "CONFIG_FROM_S3FILE"


def get_sqs_client() -> BotoBaseClient:
    """
    Getter for sqs client
    Extracted for mocking
    """
    return boto3.client("sqs")


def capture_serverless(
    func: Callable[[dict[str, Any], context_.Context], str]
) -> Callable[[dict[str, Any], context_.Context], str]:
    """
    Decorator with logic regarding when to inject apm_capture_serverless
    decorator: apm_capture_serverless expects handler to be run in a lambda
    and bew always active. We inject apm_capture_serverless decorator only if
    env variable ELASTIC_APM_ACTIVE is set and we are running in a real lambda:
    this allows us to run the handler locally or in different environment.
    """
    if "ELASTIC_APM_ACTIVE" not in os.environ or "AWS_LAMBDA_FUNCTION_NAME" not in os.environ:

        def wrapper(lambda_event: dict[str, Any], lambda_context: context_.Context) -> str:
            return func(lambda_event, lambda_context)

        return wrapper

    os.environ["ELASTIC_APM_COLLECT_LOCAL_VARIABLES"] = "off"
    return apm_capture_serverless()(func)  # type:ignore


class TriggerTypeException(Exception):
    """Raised when there is an error related to the trigger type"""

    pass


class ConfigFileException(Exception):
    """Raised when there is an error related to the config file"""

    pass


class InputConfigException(Exception):
    """Raised when there is an error related to the configured input"""

    pass


class OutputConfigException(Exception):
    """Raised when there is an error related to the configured output"""

    pass


def wrap_try_except(
    func: Callable[[dict[str, Any], context_.Context], str]
) -> Callable[[dict[str, Any], context_.Context], str]:
    """
    Decorator to catch every exception and capture them by apm client if set
    or raise if type is of between TriggerTypeException, ConfigFileException,
    InputConfigException or OutputConfigException
    """

    def wrapper(lambda_event: dict[str, Any], lambda_context: context_.Context) -> str:
        apm_client: Client = get_client()
        try:
            return func(lambda_event, lambda_context)

        # NOTE: for all these cases we want the exception to bubble up to Lambda platform and let the defined retry
        # mechanism take action. These are non transient unrecoverable error from this code point of view.
        except (ConfigFileException, InputConfigException, OutputConfigException, TriggerTypeException) as e:
            if apm_client:
                apm_client.capture_exception()

            shared_logger.exception("exception raised", exc_info=e)

            raise e

        # NOTE: any generic exception is logged and suppressed to prevent the entire Lambda function to fail.
        # As Lambda can process multiple events, when within a Lambda execution only some event produce an Exception
        # it should not prevent all other events to be ingested.
        except Exception as e:
            if apm_client:
                apm_client.capture_exception()

            shared_logger.exception("exception raised", exc_info=e)

            return f"exception raised: {e.__repr__()}"

    return wrapper


def get_shipper_and_input(
    config: Config, config_yaml: str, trigger_type: str, lambda_event: dict[str, Any]
) -> tuple[CompositeShipper, Input]:

    event_input = config.get_input_by_type_and_id(trigger_type, lambda_event["Records"][0]["eventSourceARN"])
    if not event_input:
        shared_logger.error(f'no input set for {lambda_event["Records"][0]["eventSourceARN"]}')

        raise InputConfigException("not input set")

    shared_logger.info("input", extra={"type": event_input.type, "id": event_input.id})

    composite_shipper: CompositeShipper = CompositeShipper()

    for output_type in event_input.get_output_types():
        if output_type == "elasticsearch":
            shared_logger.info("setting ElasticSearch shipper")
            output: Optional[Output] = event_input.get_output_by_type("elasticsearch")
            if output is None:
                raise OutputConfigException("no available output for elasticsearch type")

            try:
                shipper: ElasticsearchShipper = ShipperFactory.create_from_output(
                    output_type="elasticsearch", output=output
                )
                shipper.discover_dataset(event=lambda_event)
                composite_shipper.add_shipper(shipper=shipper)
                replay_handler = ReplayEventHandler(config_yaml=config_yaml, event_input=event_input)
                composite_shipper.set_replay_handler(replay_handler=replay_handler.replay_handler)

                if trigger_type == "s3-sqs":
                    composite_shipper.set_event_id_generator(event_id_generator=s3_object_id)
                elif trigger_type == "kinesis-data-stream":
                    composite_shipper.set_event_id_generator(event_id_generator=kinesis_record_id)
            except Exception as e:
                raise OutputConfigException(e)

    return composite_shipper, event_input


def config_yaml_from_payload(lambda_event: dict[str, Any]) -> str:
    """
    Extract the config yaml from sqs record message attributes.
    In case we are in a sqs continuing handler scenario we use the config
    we set when sending the sqs continuing message instead of the one defined
    from env variable
    """

    payload = lambda_event["Records"][0]["messageAttributes"]
    config_yaml: str = payload["config"]["stringValue"]

    return config_yaml


def config_yaml_from_s3() -> str:
    """
    Extract the config yaml downloading it from S3
    It is the default behaviour: reference to the config file is given
    by env variable S3_CONFIG_FILE
    """

    config_file = os.getenv("S3_CONFIG_FILE")
    assert config_file is not None

    bucket_name, object_key = from_s3_uri_to_bucket_name_and_object_key(config_file)
    shared_logger.info("config file", extra={"bucket_name": bucket_name, "object_key": object_key})

    config_storage: CommonStorage = StorageFactory.create(
        storage_type="s3", bucket_name=bucket_name, object_key=object_key
    )

    config_yaml: str = config_storage.get_as_string()
    return config_yaml


def from_s3_uri_to_bucket_name_and_object_key(s3_uri: str) -> tuple[str, str]:
    """
    Helpers for extracting bucket name and object key given an S3 URI
    """

    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid s3 uri provided: `{s3_uri}`")

    stripped_s3_uri = s3_uri.strip("s3://")

    bucket_name_and_object_key = stripped_s3_uri.split("/", 1)
    if len(bucket_name_and_object_key) < 2:
        raise ValueError(f"Invalid s3 uri provided: `{s3_uri}`")

    return bucket_name_and_object_key[0], "/".join(bucket_name_and_object_key[1:])


def get_bucket_name_from_arn(bucket_arn: str) -> str:
    """
    Helpers for extracting bucket name from a bucket ARN
    """

    return bucket_arn.split(":")[-1]


def get_kinesis_stream_name_type_and_region_from_arn(kinesis_stream_arn: str) -> tuple[str, str, str]:
    """
    Helpers for extracting stream name and type and region from a kinesis stream ARN
    """

    arn_components = kinesis_stream_arn.split(":")
    stream_componets = arn_components[-1].split("/")
    return stream_componets[0], stream_componets[1], arn_components[3]


def get_trigger_type_and_config_source(event: dict[str, Any]) -> tuple[str, str]:
    """
    Determines the trigger type according to the payload of the trigger event
    and if the config must be read from attributes or from S3 file in env
    """

    if "Records" not in event or len(event["Records"]) < 1:
        raise Exception("Not supported trigger")

    if "body" in event["Records"][0]:
        event_body = event["Records"][0]["body"]
        if "output_type" in event_body and "output_args" in event_body and "event_payload" in event_body:
            return "replay-sqs", CONFIG_FROM_PAYLOAD

    if "eventSource" not in event["Records"][0]:
        raise Exception("Not supported trigger")

    event_source = event["Records"][0]["eventSource"]
    if event_source not in _available_triggers:
        raise Exception("Not supported trigger")

    trigger_type = _available_triggers[event_source]
    if (
        trigger_type == "kinesis-data-stream"
        and "kinesis" not in event["Records"][0]
        and "data" not in event["Records"][0]["kinesis"]
    ):
        raise Exception("Not supported trigger")

    if trigger_type != "s3-sqs":
        return trigger_type, CONFIG_FROM_S3FILE

    if "messageAttributes" not in event["Records"][0]:
        return trigger_type, CONFIG_FROM_S3FILE

    if "originalEventSource" not in event["Records"][0]["messageAttributes"]:
        return trigger_type, CONFIG_FROM_S3FILE

    return "s3-sqs", CONFIG_FROM_PAYLOAD


class ReplayEventHandler:
    def __init__(self, config_yaml: str, event_input: Input):
        self._config_yaml: str = config_yaml
        self._event_input_id: str = event_input.id
        self._event_input_type: str = event_input.type

    def replay_handler(self, output_type: str, output_args: dict[str, Any], event_payload: dict[str, Any]) -> None:
        sqs_replay_queue = os.environ["SQS_REPLAY_URL"]

        sqs_client = get_sqs_client()

        message_payload: dict[str, Any] = {
            "output_type": output_type,
            "output_args": output_args,
            "event_payload": event_payload,
            "event_input_id": self._event_input_id,
            "event_input_type": self._event_input_type,
        }

        sqs_client.send_message(
            QueueUrl=sqs_replay_queue,
            MessageBody=json.dumps(message_payload),
            MessageAttributes={
                "config": {"StringValue": self._config_yaml, "DataType": "String"},
            },
        )

        shared_logger.warning("sent to replay queue", extra=message_payload)


def s3_object_id(event_payload: dict[str, Any]) -> str:
    """
    Port of
    https://github.com/elastic/beats/blob/21dca31b6296736fa90fae39bff71f063522420f/x-pack/filebeat/input/awss3/s3_objects.go#L364-L371
    https://github.com/elastic/beats/blob/21dca31b6296736fa90fae39bff71f063522420f/x-pack/filebeat/input/awss3/s3_objects.go#L356-L358
    """
    offset: int = event_payload["fields"]["log"]["offset"]
    bucket_arn: str = event_payload["fields"]["aws"]["s3"]["bucket"]["arn"]
    object_key: str = event_payload["fields"]["aws"]["s3"]["object"]["key"]

    src: str = f"{bucket_arn}{object_key}"
    hex_prefix = hashlib.sha256(src.encode("UTF-8")).hexdigest()[:10]

    return f"{hex_prefix}-{offset:012d}"


def kinesis_record_id(event_payload: dict[str, Any]) -> str:
    """
    Generates a unique event id given the payload of an event from a kinesis stream
    """
    offset: int = event_payload["fields"]["log"]["offset"]
    stream_type: str = event_payload["fields"]["aws"]["kinesis"]["type"]
    stream_name: str = event_payload["fields"]["aws"]["kinesis"]["name"]
    sequence_number: str = event_payload["fields"]["aws"]["kinesis"]["sequence_number"]

    src: str = f"{stream_type}{stream_name}-{sequence_number}"
    hex_prefix = hashlib.sha256(src.encode("UTF-8")).hexdigest()[:10]

    return f"{hex_prefix}-{offset:012d}"
