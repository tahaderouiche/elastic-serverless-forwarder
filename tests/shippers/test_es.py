# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.

import datetime
import json
from copy import deepcopy
from typing import Any
from unittest import TestCase

import elasticsearch
import mock
import pytest

from shippers import ElasticsearchShipper

_now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
_dummy_lambda_event: dict[str, Any] = {
    "Records": [
        {
            "messageId": "dummy_message_id",
            "receiptHandle": "dummy_receipt_handle",
            "body": json.dumps(
                {
                    "Records": [
                        {
                            "eventVersion": "2.1",
                            "eventSource": "aws:s3",
                            "awsRegion": "eu-central-1",
                            "eventTime": _now,
                            "eventName": "ObjectCreated:Put",
                            "userIdentity": {"principalId": "dummy_principal_id"},
                            "requestParameters": {"sourceIPAddress": "dummy_source_ip_address"},
                            "responseElements": {
                                "x-amz-request-id": "dummy_request_id",
                                "x-amz-id-2": "dummy_request_id_2",
                            },
                            "s3": {
                                "s3SchemaVersion": "1.0",
                                "configurationId": "sqs_event",
                                "bucket": {
                                    "name": "dummy_bucket_name",
                                    "ownerIdentity": {"principalId": "dummy_principal_id"},
                                    "arn": "arn:aws:s3:::dummy_bucket_name",
                                },
                                "object": {
                                    "key": "file.log",
                                    "size": 27,
                                    "eTag": "",
                                    "sequencer": "",
                                },
                            },
                        }
                    ]
                }
            ),
            "attributes": {
                "ApproximateReceiveCount": "1",
                "SentTimestamp": _now,
                "SenderId": "dummy_sender_id",
                "ApproximateFirstReceiveTimestamp": _now,
            },
            "messageAttributes": {},
            "md5OfBody": "dummy_hash",
            "eventSource": "aws:sqs",
            "eventSourceARN": "dummy_source_arn",
            "awsRegion": "eu-central-1",
        }
    ]
}
_dummy_event: dict[str, Any] = {
    "@timestamp": _now,
    "fields": {
        "message": "A dummy message",
        "log": {
            "offset": 10,
            "file": {
                "path": "https://bucket_name.s3.aws-region.amazonaws.com/file.key",
            },
        },
        "aws": {
            "s3": {
                "bucket": {
                    "name": "arn:aws:s3:::bucket_name",
                    "arn": "bucket_name",
                },
                "object": {
                    "key": "file.key",
                },
            },
        },
        "cloud": {
            "provider": "aws",
            "region": "aws-region",
        },
    },
}


class MockTransport(elasticsearch.Transport):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


class MockClient(elasticsearch.Elasticsearch):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport_class"] = MockTransport
        super().__init__(*args, **kwargs)


_documents = []
_failures = []


def mock_bulk(client: Any, actions: list[dict[str, Any]], **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
    global _documents
    _documents = [actions]
    return len(actions), []


def mock_bulk_failure(client: Any, actions: list[dict[str, Any]], **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
    global _failures
    _failures = list(map(lambda action: {"create": {"_id": action["_id"]}}, actions))
    return len(actions), _failures


@pytest.mark.unit
class TestElasticsearchShipper(TestCase):
    @mock.patch("shippers.es.es_bulk", mock_bulk)
    @mock.patch("shippers.es.Elasticsearch", new=MockClient)
    def test_send(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="",
            es_index_or_datastream_name="logs-data.set-namespace",
            tags=["tag1", "tag2", "tag3"],
            batch_max_actions=0,
        )
        es_event = deepcopy(_dummy_event)
        shipper.discover_dataset(es_event)
        shipper.send(es_event)

        assert _documents == [
            [
                {
                    "@timestamp": _now,
                    "_index": "logs-data.set-namespace",
                    "_op_type": "create",
                    "data_stream": {"dataset": "data.set", "namespace": "namespace", "type": "logs"},
                    "event": {"dataset": "data.set", "original": "A dummy message"},
                    "fields": {
                        "aws": {
                            "s3": {
                                "bucket": {"arn": "bucket_name", "name": "arn:aws:s3:::bucket_name"},
                                "object": {"key": "file.key"},
                            }
                        },
                        "cloud": {"provider": "aws", "region": "aws-region"},
                        "log": {
                            "file": {"path": "https://bucket_name.s3.aws-region.amazonaws.com/file.key"},
                            "offset": 10,
                        },
                        "message": "A dummy message",
                    },
                    "tags": [
                        "preserve_original_event",
                        "forwarded",
                        "data-set",
                        "tag1",
                        "tag2",
                        "tag3",
                    ],
                }
            ]
        ]
        assert shipper._bulk_actions == []

    @mock.patch("shippers.es.es_bulk", mock_bulk_failure)
    @mock.patch("shippers.es.Elasticsearch", new=MockClient)
    def test_send_with_failure(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="",
            es_index_or_datastream_name="data.set",
            tags=["tag1", "tag2", "tag3"],
            batch_max_actions=0,
        )
        es_event = deepcopy(_dummy_event)
        shipper.discover_dataset(es_event)

        def event_id_generator(event: dict[str, Any]) -> str:
            return "_id"

        shipper.set_event_id_generator(event_id_generator=event_id_generator)
        shipper.send(es_event)

        assert _failures == [{"create": {"_id": "_id"}}]
        assert shipper._bulk_actions == []

    @mock.patch("shippers.es.es_bulk", mock_bulk)
    @mock.patch("shippers.es.Elasticsearch", new=MockClient)
    def test_es_index_empty(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )
        es_event = deepcopy(_dummy_event)

        with self.assertRaisesRegex(ValueError, "Elasticsearch index cannot be empty"):
            shipper.send(es_event)

    @mock.patch("shippers.es.es_bulk", mock_bulk)
    @mock.patch("shippers.es.Elasticsearch", new=MockClient)
    def test_flush(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="",
            es_index_or_datastream_name="logs-data.set-namespace",
            tags=["tag1", "tag2", "tag3"],
            batch_max_actions=2,
        )
        es_event = deepcopy(_dummy_event)
        shipper.discover_dataset(es_event)
        shipper.send(es_event)

        assert shipper._bulk_actions == [
            {
                "@timestamp": _now,
                "_index": "logs-data.set-namespace",
                "_op_type": "create",
                "data_stream": {"dataset": "data.set", "namespace": "namespace", "type": "logs"},
                "event": {"dataset": "data.set", "original": "A dummy message"},
                "fields": {
                    "aws": {
                        "s3": {
                            "bucket": {"arn": "bucket_name", "name": "arn:aws:s3:::bucket_name"},
                            "object": {"key": "file.key"},
                        }
                    },
                    "cloud": {"provider": "aws", "region": "aws-region"},
                    "log": {"file": {"path": "https://bucket_name.s3.aws-region.amazonaws.com/file.key"}, "offset": 10},
                    "message": "A dummy message",
                },
                "tags": [
                    "preserve_original_event",
                    "forwarded",
                    "data-set",
                    "tag1",
                    "tag2",
                    "tag3",
                ],
            }
        ]

        shipper.flush()

        assert shipper._bulk_actions == []

        shipper.flush()

    @mock.patch("shippers.es.es_bulk", mock_bulk)
    @mock.patch("shippers.es.Elasticsearch", new=MockClient)
    def test_discover_dataset(self) -> None:
        with self.subTest("empty es_index_or_datastream_name"):
            shipper = ElasticsearchShipper(
                elasticsearch_url="elasticsearch_url",
                username="username",
                password="password",
                tags=["tag1", "tag2", "tag3"],
                batch_max_actions=0,
            )
            es_event = deepcopy(_dummy_event)
            lambda_event = deepcopy(_dummy_lambda_event)
            shipper.discover_dataset(lambda_event)
            shipper.send(es_event)

            assert shipper._dataset == "generic"
            assert shipper._namespace == "default"
            assert shipper._es_index == "logs-generic-default"

            assert _documents[0] == [
                {
                    "@timestamp": _now,
                    "_index": "logs-generic-default",
                    "_op_type": "create",
                    "data_stream": {"dataset": "generic", "namespace": "default", "type": "logs"},
                    "event": {"dataset": "generic", "original": "A dummy message"},
                    "fields": {
                        "aws": {
                            "s3": {
                                "bucket": {"arn": "bucket_name", "name": "arn:aws:s3:::bucket_name"},
                                "object": {"key": "file.key"},
                            }
                        },
                        "cloud": {"provider": "aws", "region": "aws-region"},
                        "log": {
                            "file": {"path": "https://bucket_name.s3.aws-region.amazonaws.com/file.key"},
                            "offset": 10,
                        },
                        "message": "A dummy message",
                    },
                    "tags": [
                        "preserve_original_event",
                        "forwarded",
                        "generic",
                        "tag1",
                        "tag2",
                        "tag3",
                    ],
                }
            ]

            assert shipper._bulk_actions == []

        with self.subTest("es_index_or_datastream_name as `logs-unit-test"):
            shipper = ElasticsearchShipper(
                elasticsearch_url="elasticsearch_url",
                username="username",
                password="password",
                es_index_or_datastream_name="logs-unit-test",
                tags=["tag1", "tag2", "tag3"],
                batch_max_actions=0,
            )
            es_event = deepcopy(_dummy_event)
            lambda_event = deepcopy(_dummy_lambda_event)
            shipper.discover_dataset(lambda_event)
            shipper.send(es_event)

            assert shipper._dataset == "unit"
            assert shipper._namespace == "test"
            assert shipper._es_index == "logs-unit-test"

            assert _documents[0] == [
                {
                    "@timestamp": _now,
                    "_index": "logs-unit-test",
                    "_op_type": "create",
                    "data_stream": {"dataset": "unit", "namespace": "test", "type": "logs"},
                    "event": {"dataset": "unit", "original": "A dummy message"},
                    "fields": {
                        "aws": {
                            "s3": {
                                "bucket": {"arn": "bucket_name", "name": "arn:aws:s3:::bucket_name"},
                                "object": {"key": "file.key"},
                            }
                        },
                        "cloud": {"provider": "aws", "region": "aws-region"},
                        "log": {
                            "file": {"path": "https://bucket_name.s3.aws-region.amazonaws.com/file.key"},
                            "offset": 10,
                        },
                        "message": "A dummy message",
                    },
                    "tags": [
                        "preserve_original_event",
                        "forwarded",
                        "unit",
                        "tag1",
                        "tag2",
                        "tag3",
                    ],
                }
            ]

            assert shipper._bulk_actions == []

        with self.subTest("es_index_or_datastream_name not matching logs datastream naming conventation"):
            shipper = ElasticsearchShipper(
                elasticsearch_url="elasticsearch_url",
                username="username",
                password="password",
                es_index_or_datastream_name="es_index_or_datastream_name",
                tags=["tag1", "tag2", "tag3"],
                batch_max_actions=0,
            )
            es_event = deepcopy(_dummy_event)
            lambda_event = deepcopy(_dummy_lambda_event)
            shipper.discover_dataset(lambda_event)
            shipper.send(es_event)

            assert shipper._dataset == ""
            assert shipper._namespace == ""
            assert shipper._es_index == "es_index_or_datastream_name"

            assert _documents[0] == [
                {
                    "@timestamp": _now,
                    "_index": "es_index_or_datastream_name",
                    "_op_type": "create",
                    "event": {"original": "A dummy message"},
                    "fields": {
                        "aws": {
                            "s3": {
                                "bucket": {"arn": "bucket_name", "name": "arn:aws:s3:::bucket_name"},
                                "object": {"key": "file.key"},
                            }
                        },
                        "cloud": {"provider": "aws", "region": "aws-region"},
                        "log": {
                            "file": {"path": "https://bucket_name.s3.aws-region.amazonaws.com/file.key"},
                            "offset": 10,
                        },
                        "message": "A dummy message",
                    },
                    "tags": [
                        "preserve_original_event",
                        "forwarded",
                        "tag1",
                        "tag2",
                        "tag3",
                    ],
                }
            ]

            assert shipper._bulk_actions == []


@pytest.mark.unit
class TestDiscoverDataset(TestCase):
    def test_custom_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            es_index_or_datastream_name="logs-dataset-namespace",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "dataset"
        assert shipper._namespace == "namespace"
        assert shipper._es_index == "logs-dataset-namespace"

    def test_aws_cloudtrail_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
            "AWSLogs/aws-account-id/CloudTrail/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail_region_end-time_random-string.log.gz"
        )
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.cloudtrail"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.cloudtrail-default"

    def test_aws_cloudtrail_digest_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
            "AWSLogs/aws-account-id/CloudTrail-Digest/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail-Digest_region_end-time_random-string.log.gz"
        )
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.cloudtrail"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.cloudtrail-default"

    def test_aws_cloudtrail_insight_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
            "AWSLogs/aws-account-id/CloudTrail-Insight/region/yyyy/mm/dd/"
            "aws-account-id_CloudTrail-Insight_region_end-time_random-string.log.gz"
        )
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.cloudtrail"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.cloudtrail-default"

    def test_aws_cloudwatch_logs_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = "exportedlogs/111-222-333/2021-12-28/hash/file.gz"
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.cloudwatch_logs"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.cloudwatch_logs-default"

    def test_aws_elb_logs_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
            "AWSLogs/aws-account-id/elasticloadbalancing/region/yyyy/mm/dd/"
            "aws-account-id_elasticloadbalancing_region_load-balancer-id_end-time_ip-address_random-string.log.gz"
        )
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.elb_logs"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.elb_logs-default"

    def test_aws_network_firewall_logs_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"][
            "key"
        ] = "AWSLogs/aws-account-id/network-firewall/log-type/Region/firewall-name/timestamp/"
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.firewall_logs"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.firewall_logs-default"

    def test_aws_lambda_logs_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = "prefix/111-222-333/lambda/2021-12-28/hash/file.gz"
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.lambda"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.lambda-default"

    def test_aws_sns_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"][
            "key"
        ] = "<my-s3-bucket>/SMSUsageReports/<region>/YYYY/MM/DD/00x.csv.gz"
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.sns"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.sns-default"

    def test_aws_s3_storage_lens_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
            "DestinationPrefix/StorageLens/123456789012/my-dashboard-configuration-id/V_1/reports"
            "/dt=2020-11-03/bd23de7c-b46a-4cf4-bcc5-b21aac5be0f5.par"
        )
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.s3_storage_lens"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.s3_storage_lens-default"

    def test_aws_waf_logs_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"][
            "key"
        ] = "AWSLogs/account-id/WAFLogs/Region/web-acl-name/YYYY/MM/dd/HH/mm"
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.waf"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.waf-default"

    def test_aws_vpcflow_logs_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = (
            "AWSLogs/id/vpcflowlogs/region/" "date_vpcflowlogs_region_file.log.gz"
        )
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "aws.vpcflow"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-aws.vpcflow-default"

    def test_unknown_dataset(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = "random_hash"
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "generic"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-generic-default"

    def test_records_not_in_event(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        del lambda_event_body["Records"]
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)
        print(lambda_event)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "generic"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-generic-default"

    def test_s3_key_not_in_records(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = {"Records": [{"body": '{"Records": [{}]}'}]}

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "generic"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-generic-default"

    def test_empty_s3_key(self) -> None:
        shipper = ElasticsearchShipper(
            elasticsearch_url="elasticsearch_url",
            username="username",
            password="password",
            tags=["tag1", "tag2", "tag3"],
        )

        lambda_event = deepcopy(_dummy_lambda_event)
        lambda_event_body = json.loads(lambda_event["Records"][0]["body"])
        lambda_event_body["Records"][0]["s3"]["object"]["key"] = ""
        lambda_event["Records"][0]["body"] = json.dumps(lambda_event_body)

        shipper.discover_dataset(lambda_event)

        assert shipper._dataset == "generic"
        assert shipper._namespace == "default"
        assert shipper._es_index == "logs-generic-default"
