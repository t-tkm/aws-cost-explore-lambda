import os
import pytest
import requests
from unittest.mock import MagicMock, patch
import botocore.exceptions

import app as cost_report


@pytest.fixture
def mock_ce_client():
    return MagicMock()


@pytest.fixture
def explorer(mock_ce_client):
    return cost_report.CostExplorer(mock_ce_client)


@pytest.fixture
def sample_cost_response():
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-28"},
                "Total": {cost_report.COST_METRIC: {"Amount": "123.45"}},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2"],
                        "Metrics": {cost_report.COST_METRIC: {"Amount": "100.0"}},
                    },
                    {
                        "Keys": ["Amazon S3"],
                        "Metrics": {cost_report.COST_METRIC: {"Amount": "23.45"}},
                    },
                ],
            }
        ]
    }


def test_get_account_id_failure():
    with patch.object(cost_report.boto3, "client") as mock_client:
        mock_sts = MagicMock()
        mock_client.return_value = mock_sts
        mock_sts.get_caller_identity.side_effect = botocore.exceptions.ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            operation_name="GetCallerIdentity",
        )
        with pytest.raises(RuntimeError) as exc:
            cost_report.get_account_id()
        assert "AWS Account IDの取得に失敗しました。" in str(exc.value)


@pytest.mark.parametrize(
    "use_teams, webhook_url, expect_error, expect_post_calls",
    [
        (True, "https://dummy.webhook.microsoft.com/xxxx", False, 2),
        (True, None, True, 0),
        (False, None, False, 0),
    ],
)
@patch.object(cost_report.boto3, "client")
def test_main(mock_boto3_client, use_teams, webhook_url, expect_error, expect_post_calls):
    mock_ce = MagicMock()
    mock_boto3_client.return_value = mock_ce
    mock_ce.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {"Total": {cost_report.COST_METRIC: {"Amount": "100.0"}}, "Groups": []}
        ]
    }

    env_dict = {"USE_TEAMS_POST": "yes" if use_teams else "no"}
    if webhook_url:
        env_dict["TEAMS_WEBHOOK_URL"] = webhook_url

    with patch.dict(os.environ, env_dict, clear=True):
        with patch.object(cost_report, "post_to_teams") as mock_post:
            with patch.object(cost_report, "print_report") as mock_print:
                if expect_error:
                    with pytest.raises(ValueError):
                        cost_report.main()
                else:
                    cost_report.main()
                    assert mock_post.call_count == expect_post_calls
                    assert mock_print.call_count == 2


@pytest.mark.parametrize(
    "include_credit, extra_ce_kwargs",
    [
        (True, {}),
        (
            False,
            {
                "Filter": {
                    "Not": {
                        "Dimensions": {
                            "Key": cost_report.RECORD_TYPE_DIMENSION,
                            "Values": [cost_report.CREDIT_RECORD_TYPE],
                        }
                    }
                }
            },
        ),
    ],
)
def test_get_cost_and_usage_credit_filter(
    explorer, mock_ce_client, sample_cost_response, include_credit, extra_ce_kwargs
):
    mock_ce_client.get_cost_and_usage.return_value = sample_cost_response
    period = {"Start": "2024-12-01", "End": "2024-12-28"}

    got = explorer.get_cost_and_usage(period, include_credit=include_credit)

    assert got == sample_cost_response["ResultsByTime"][0]
    mock_ce_client.get_cost_and_usage.assert_called_once_with(
        TimePeriod=period,
        Granularity=cost_report.GRANULARITY,
        Metrics=[cost_report.COST_METRIC],
        GroupBy=[],
        **extra_ce_kwargs,
    )


@patch.object(cost_report.requests, "Session")
def test_post_to_teams_falls_back_to_text_on_adaptive_failure(mock_session_cls):
    mock_session = MagicMock()
    mock_session_cls.return_value = mock_session
    
    err = requests.HTTPError()
    err.response = MagicMock(status_code=400, text="adaptive rejected")
    
    bad = MagicMock()
    bad.raise_for_status.side_effect = err
    
    good = MagicMock()
    good.raise_for_status = MagicMock()
    
    mock_session.post.side_effect = [bad, good]

    assert cost_report.post_to_teams("タイトル", ["- S3: 1.00 USD"], "https://hooks.example/webhook")
    assert mock_session.post.call_count == 2
    second_kw = mock_session.post.call_args_list[1][1]
    assert "json" in second_kw
    assert second_kw["json"]["text"].startswith("タイトル")


def test_lambda_handler_success():
    with patch.object(cost_report, "main") as mock_main:
        resp = cost_report.lambda_handler({}, None)
        assert resp["statusCode"] == 200
        assert "successfully" in resp["body"]
        mock_main.assert_called_once()


def test_lambda_handler_failure():
    with patch.object(cost_report, "main") as mock_main:
        mock_main.side_effect = Exception("test error")
        resp = cost_report.lambda_handler({}, None)
        assert resp["statusCode"] == 500
        assert "test error" in resp["body"]
