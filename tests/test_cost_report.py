import os
import pytest
import requests
from unittest.mock import MagicMock, patch
import botocore.exceptions

import app as cost_report


@pytest.fixture
def mock_ce_client():
    """
    boto3 CE クライアント (self.client) をモックにした MagicMock を返すフィクスチャ。
    """
    return MagicMock()


@pytest.fixture
def explorer(mock_ce_client):
    """
    コスト取得クラスを生成して返すフィクスチャ。
    """
    return cost_report.CostExplorer(mock_ce_client)


@pytest.fixture
def sample_cost_response():
    """
    サンプルの get_cost_and_usage レスポンスを返すフィクスチャ。
    """
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-28"},
                "Total": {cost_report.COST_METRIC: {"Amount": "123.45"}},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2"],
                        "Metrics": {cost_report.COST_METRIC: {"Amount": "100.0"}}
                    },
                    {
                        "Keys": ["Amazon S3"],
                        "Metrics": {cost_report.COST_METRIC: {"Amount": "23.45"}}
                    }
                ]
            }
        ]
    }


def test_get_account_id_success():
    """
    正常系: AWSアカウントIDを正しく取得できるかテスト。
    """
    with patch.object(cost_report.boto3, "client") as mock_client:
        # モックされたSTSクライアントの設定
        mock_sts_client = MagicMock()
        mock_client.return_value = mock_sts_client
        mock_sts_client.get_caller_identity.return_value = {"Account": "123456789012"}
        
        # 関数呼び出しと結果の検証
        account_id = cost_report.get_account_id()
        assert account_id == "123456789012"
        mock_sts_client.get_caller_identity.assert_called_once()


def test_get_account_id_failure():
    """
    異常系: get_caller_identityがエラーをスローした場合の動作をテスト。
    """
    with patch.object(cost_report.boto3, "client") as mock_client:
        # モックされたSTSクライアントの設定
        mock_sts_client = MagicMock()
        mock_client.return_value = mock_sts_client
        mock_sts_client.get_caller_identity.side_effect = botocore.exceptions.ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            operation_name="GetCallerIdentity"
        )
        
        # エラーハンドリングの検証
        with pytest.raises(RuntimeError) as exc:
            cost_report.get_account_id()
        assert "AWS Account IDの取得に失敗しました。" in str(exc.value)
        mock_sts_client.get_caller_identity.assert_called_once()


@pytest.mark.parametrize(
    "use_teams, webhook_url, expect_error, expect_post_calls",
    [
        # 1) Teams投稿あり (クレジット後/前で2回 post_to_teams が呼ばれる想定)
        (True, "https://dummy.webhook.microsoft.com/xxxx", False, 2),
        # 2) Teams投稿ON かつ webhook URL なし => ValueError
        (True, None, True, 0),
        # 3) Teams投稿OFF => post_to_teamsは一切呼ばれない (0回)
        (False, None, False, 0),
    ],
)
@patch.object(cost_report.boto3, "client")
def test_main(mock_boto3_client, use_teams, webhook_url, expect_error, expect_post_calls):
    """
    main() 関数での一連のフローをテスト。
    """
    # boto3 クライアントのモック
    mock_ce_client = MagicMock()
    mock_boto3_client.return_value = mock_ce_client

    # サンプルレスポンス
    mock_ce_client.get_cost_and_usage.return_value = {
        "ResultsByTime": [
            {"Total": {cost_report.COST_METRIC: {"Amount": "100.0"}}, "Groups": []}
        ]
    }

    # 環境変数設定
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


def test_get_cost_and_usage_include_credit(explorer, mock_ce_client, sample_cost_response):
    """
    include_credit=True の場合、フィルタなしでAPIを呼び出すかどうかをテスト。
    """
    mock_ce_client.get_cost_and_usage.return_value = sample_cost_response
    period = {"Start": "2024-12-01", "End": "2024-12-28"}

    resp = explorer.get_cost_and_usage(period, include_credit=True)

    assert resp == sample_cost_response["ResultsByTime"][0]
    mock_ce_client.get_cost_and_usage.assert_called_once_with(
        TimePeriod=period,
        Granularity=cost_report.GRANULARITY,
        Metrics=[cost_report.COST_METRIC],
        GroupBy=[]
    )


def test_get_cost_and_usage_exclude_credit(explorer, mock_ce_client, sample_cost_response):
    """
    include_credit=False の場合、Not フィルタが使われるかをテスト。
    """
    mock_ce_client.get_cost_and_usage.return_value = sample_cost_response
    period = {"Start": "2024-12-01", "End": "2024-12-28"}

    resp = explorer.get_cost_and_usage(period, include_credit=False)

    assert resp == sample_cost_response["ResultsByTime"][0]
    expected_filter = {
        "Filter": {
            "Not": {
                "Dimensions": {
                    "Key": cost_report.RECORD_TYPE_DIMENSION,
                    "Values": [cost_report.CREDIT_RECORD_TYPE]
                }
            }
        }
    }
    mock_ce_client.get_cost_and_usage.assert_called_once_with(
        TimePeriod=period,
        Granularity=cost_report.GRANULARITY,
        Metrics=[cost_report.COST_METRIC],
        GroupBy=[],
        **expected_filter
    )


@patch.object(cost_report.requests, "post")
def test_post_to_teams_falls_back_to_text_on_adaptive_failure(mock_post):
    """legacy_adaptive が 4xx などで失敗したら {\"text\": ...} を送る。"""
    err = requests.HTTPError()
    err.response = MagicMock(status_code=400, text="adaptive rejected")
    bad = MagicMock()
    bad.raise_for_status.side_effect = err
    good = MagicMock()
    good.raise_for_status = MagicMock()
    mock_post.side_effect = [bad, good]

    assert cost_report.post_to_teams("タイトル", ["- S3: 1.00 USD"], "https://hooks.example/webhook")
    assert mock_post.call_count == 2
    second_kw = mock_post.call_args_list[1][1]
    assert "json" in second_kw
    assert second_kw["json"]["text"].startswith("タイトル")
