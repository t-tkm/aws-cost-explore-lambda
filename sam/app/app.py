import os
import json
import logging
from datetime import datetime, timedelta, date
from typing import Tuple, List, Dict, Any, Optional
from urllib.parse import urlparse

import boto3
import botocore.client
import botocore.exceptions
import requests

# --------------------------------------------------------------------
# 定数定義
# --------------------------------------------------------------------
REGION_NAME = "us-east-1"
GRANULARITY = "MONTHLY"
COST_METRIC = "AmortizedCost"
SERVICE_GROUP_DIMENSION = "SERVICE"
RECORD_TYPE_DIMENSION = "RECORD_TYPE"
CREDIT_RECORD_TYPE = "Credit"
MIN_BILLING_THRESHOLD = 0.01
TEAMS_REQUEST_TIMEOUT_SEC = 10  # P0-2: Webhook POSTのタイムアウト（秒）

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_webhook_url(raw: str) -> str:
    """
    Secrets Manager に JSON で保存している場合（例: {\"url\":\"https://...\"}）に対応する。
    平文の URL のときはそのまま返す。
    """
    text = (raw or "").strip()
    if not text.startswith("{"):
        return text
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for key in ("webhook_url", "url", "TEAMS_WEBHOOK_URL", "value"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip().startswith("http"):
                    return v.strip()
    except json.JSONDecodeError:
        pass
    return text


# --------------------------------------------------------------------
# 実行時に環境変数を取得する関数
# --------------------------------------------------------------------
def get_config() -> dict:
    """
    環境変数を実行時に取得して返す。
    Teams 通知が有効な場合、Webhook URL は次のいずれかから取得する。

    1. TEAMS_WEBHOOK_URL … ローカル実行向け（平文のため Lambda では使わない想定）
    2. TEAMS_SECRET_ARN … Lambda 向け（Secrets Manager に URL を保存）

    Returns:
        dict: USE_TEAMS_POST, TEAMS_WEBHOOK_URL をキーに含む辞書

    Raises:
        ValueError: Teams 有効だが URL の取得元がどちらも無い場合
        RuntimeError: Secrets Manager からの取得に失敗した場合
    """
    use_teams = os.environ.get("USE_TEAMS_POST", "no").lower() == "yes"
    webhook_url: Optional[str] = None

    if use_teams:
        direct = (os.environ.get("TEAMS_WEBHOOK_URL") or "").strip()
        if direct:
            logger.info("Teams Webhook URL は環境変数 TEAMS_WEBHOOK_URL を使用します（ローカル向け）。")
            webhook_url = _parse_webhook_url(direct)
        else:
            secret_arn = (os.environ.get("TEAMS_SECRET_ARN") or "").strip()
            if not secret_arn:
                raise ValueError(
                    "Teams 通知を有効にするには、TEAMS_WEBHOOK_URL（ローカル）または "
                    "TEAMS_SECRET_ARN（Secrets Manager の ARN）のいずれかを設定してください。"
                    " ターミナルでは必ず export してください（export なしの代入は echo では見えても "
                    "uv run の子プロセスには渡りません）。"
                )
            try:
                sm_client = boto3.client("secretsmanager")
                secret = sm_client.get_secret_value(SecretId=secret_arn)
                raw = (secret.get("SecretString") or "").strip()
                webhook_url = _parse_webhook_url(raw)
                logger.info("Secrets Manager から Teams Webhook URL を取得しました。")
            except botocore.exceptions.ClientError as e:
                logger.error(f"Secrets Manager からの取得に失敗しました: {e}")
                raise RuntimeError("Teams Webhook URL の取得に失敗しました。") from e

        if not webhook_url:
            raise ValueError("Teams Webhook URL が空です。Secrets の値または TEAMS_WEBHOOK_URL を確認してください。")

        parsed = urlparse(webhook_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "Teams Webhook URL が無効です（http(s):// で始まる実際の URL ではありません）。"
                "README のプレースホルダー「（Webhook URL）」などをそのまま使っていないか確認し、"
                "Teams / Power Automate でコピーした https://... の文字列を設定してください。"
            )

    return {
        "USE_TEAMS_POST": use_teams,
        "TEAMS_WEBHOOK_URL": webhook_url,
    }


# --------------------------------------------------------------------
# クラス・関数定義
# --------------------------------------------------------------------
class CostExplorer:
    """
    AWS Cost Explorer API を用いてコスト情報を取得するクラス。
    """

    def __init__(self, client: botocore.client.BaseClient) -> None:
        self.client = client

    def get_cost_and_usage(
        self,
        period: Dict[str, str],
        include_credit: bool,
        group_by_dimension: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        指定期間のコストと使用状況を取得する。
        """
        try:
            filter_params: Dict[str, Any] = {}
            if not include_credit:
                filter_params = {
                    "Filter": {
                        "Not": {
                            "Dimensions": {
                                "Key": RECORD_TYPE_DIMENSION,
                                "Values": [CREDIT_RECORD_TYPE]
                            }
                        }
                    }
                }

            group_by = []
            if group_by_dimension:
                group_by = [{"Type": "DIMENSION", "Key": group_by_dimension}]

            response = self.client.get_cost_and_usage(
                TimePeriod=period,
                Granularity=GRANULARITY,
                Metrics=[COST_METRIC],
                GroupBy=group_by,
                **filter_params
            )
            return response["ResultsByTime"][0]

        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to fetch cost and usage data: {e}")
            raise RuntimeError(f"Error calling AWS Cost Explorer API: {e}") from e

    def get_total_cost(self, cost_and_usage_data: Dict[str, Any]) -> float:
        """
        コストと使用状況のデータから合計費用を取得する。
        """
        try:
            if not cost_and_usage_data.get("Total"):
                total_cost = sum(
                    max(0, float(group["Metrics"][COST_METRIC]["Amount"]))
                    for group in cost_and_usage_data.get("Groups", [])
                )
                logger.info(f"Calculated total cost from Groups: {total_cost:.2f} USD")
                return total_cost

            return float(cost_and_usage_data["Total"][COST_METRIC]["Amount"])

        except KeyError as e:
            logger.error(f"Metric '{COST_METRIC}' not found in response data: {e}")
            return 0.0

    def get_service_costs(self, cost_and_usage_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        コストと使用状況のデータからサービスごとの費用を取得する。
        """
        service_groups = cost_and_usage_data.get("Groups", [])
        result = []
        for item in service_groups:
            billing_amount = float(item["Metrics"][COST_METRIC]["Amount"])
            result.append({
                "service_name": item["Keys"][0],
                "billing": billing_amount
            })
        return result


def get_client() -> botocore.client.BaseClient:
    """
    boto3 Cost Explorer クライアントを返す。
    """
    return boto3.client("ce", region_name=REGION_NAME)


def get_date_range() -> Tuple[str, str]:
    """
    集計期間を取得する。
    """
    start_date = date.today().replace(day=1).isoformat()
    end_date = date.today().isoformat()
    return start_date, end_date


def format_service_costs(service_billings: List[Dict[str, Any]]) -> List[str]:
    """
    サービスごとの費用を表示用に整形する。
    """
    formatted_services = []
    for item in service_billings:
        billing = item["billing"]
        if billing >= MIN_BILLING_THRESHOLD:
            formatted_services.append(f"- {item['service_name']}: {billing:.2f} USD")
        else:
            logger.debug(f"Excluded negligible cost: {item['service_name']} ({billing:.5f})")
    return formatted_services


def handle_cost_report(
    explorer: CostExplorer,
    period: Dict[str, str],
    include_credit: bool,
    start_day: str,
    end_day: str
) -> Tuple[str, List[str]]:
    """
    費用レポート（クレジット適用前/後）の取得と整形を行う。
    """
    cost_and_usage = explorer.get_cost_and_usage(
        period,
        include_credit=include_credit,
        group_by_dimension=SERVICE_GROUP_DIMENSION
    )
    total_cost = explorer.get_total_cost(cost_and_usage)
    services_cost = explorer.get_service_costs(cost_and_usage)
    formatted_services = format_service_costs(services_cost)

    credit_text = "後" if include_credit else "前"
    title = f"{start_day}～{end_day}のクレジット適用{credit_text}費用は、{total_cost:.2f} USD です。"
    return title, formatted_services


def print_report(title: str, services_cost: List[str]) -> None:
    """
    レポートを標準出力に表示する。
    """
    print("------------------------------------------------------")
    print(title)
    if services_cost:
        print("\n".join(services_cost))
    else:
        print("サービスごとの費用データはありません。")
    print("------------------------------------------------------\n")


def _teams_payload_legacy_adaptive(title: str, services_text: str) -> Dict[str, Any]:
    """
    旧 src/cost_report.py で Teams に表示できていた形式と同一。
    （attachments のみ・AdaptiveCard 1.2・TextBlock に markdown: true）
    """
    return {
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"### {title}\n\n{services_text}",
                            "wrap": True,
                            "markdown": True,
                        }
                    ],
                },
            }
        ]
    }


def _teams_payload_text(title: str, services_text: str) -> Dict[str, str]:
    """Microsoft 公式 curl 例と同じ {\"text\": \"...\"}（フォールバック用）。"""
    return {"text": f"{title}\n\n{services_text}"}


def post_to_teams(title: str, services_cost: List[str], webhook_url: str) -> bool:
    """
    Teams Webhook に POST する。
    既定は旧 cost_report.py と同じ Adaptive（legacy_adaptive）。失敗時のみ {\"text\": ...}。

    TEAMS_WEBHOOK_FORMAT=text のときは text のみ送る。

    Returns:
        bool: いずれかの形式で成功すれば True。すべて失敗なら False（ログのみ）。
    """

    services_text = "\n".join(services_cost) if services_cost else "サービスごとの費用データはありません。"

    fmt = (os.environ.get("TEAMS_WEBHOOK_FORMAT") or "").strip().lower()
    if fmt == "text":
        strategies: List[Tuple[str, Dict[str, Any]]] = [
            ("text", _teams_payload_text(title, services_text)),
        ]
    else:
        strategies = [
            ("legacy_adaptive", _teams_payload_legacy_adaptive(title, services_text)),
            ("text", _teams_payload_text(title, services_text)),
        ]

    last_error: Optional[requests.exceptions.RequestException] = None
    for idx, (name, payload) in enumerate(strategies):
        try:
            if name == "legacy_adaptive":
                # 旧実装どおり json を文字列化して data= で送る（Incoming Webhook での表示実績あり）
                response = requests.post(
                    webhook_url,
                    data=json.dumps(payload, ensure_ascii=False),
                    headers={"Content-Type": "application/json"},
                    timeout=TEAMS_REQUEST_TIMEOUT_SEC,
                )
            else:
                response = requests.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    timeout=TEAMS_REQUEST_TIMEOUT_SEC,
                )
            response.raise_for_status()
            # Power Automate 等は 202 で本文が空のことが多い。HTTP 成功でもチャネル投稿は別ステップ依存。
            body_preview = (response.text or "").strip()[:400]
            if fmt == "text" and len(strategies) == 1:
                logger.info(
                    "Teams: TEAMS_WEBHOOK_FORMAT=text のため、Adaptive は送らず text のみ送信します。"
                )
            elif idx > 0:
                logger.info(
                    "Teams: 形式「%s」が失敗したため「%s」で再送し、HTTP %s になりました（直前の WARNING に失敗理由があります）。",
                    strategies[idx - 1][0],
                    name,
                    response.status_code,
                )
            logger.info(
                "Teams Webhook へ HTTP %s（形式: %s）。応答本文の先頭: %r",
                response.status_code,
                name,
                body_preview if body_preview else "(空)",
            )
            return True
        except requests.exceptions.RequestException as e:
            last_error = e
            resp = getattr(e, "response", None)
            extra = ""
            if resp is not None:
                snippet = (resp.text or "")[:800]
                extra = f" HTTP {resp.status_code} body={snippet!r}"
            logger.warning(
                "Teams Webhook（形式: %s）が失敗しました。%s",
                name,
                extra or str(e),
            )

    if last_error is not None:
        resp = getattr(last_error, "response", None)
        tail = ""
        if resp is not None:
            tail = f" HTTP {resp.status_code} body={(resp.text or '')[:800]!r}"
        logger.error(
            "Teams Webhook への通知はすべての形式で失敗しました（処理は継続します）: %s%s",
            last_error,
            tail,
        )
    return False


def get_account_id(sts_client: Optional[botocore.client.BaseClient] = None) -> str:
    """
    AWSアカウントIDを取得する。

    Args:
        sts_client: STSクライアント。省略時は boto3 デフォルトクライアントを使用。
    """
    try:
        client = sts_client or boto3.client("sts")
        account_id = client.get_caller_identity()["Account"]
        return account_id
    except botocore.exceptions.ClientError as e:
        logger.error(f"Failed to fetch AWS Account ID: {e}")
        raise RuntimeError("AWS Account IDの取得に失敗しました。") from e


def main() -> None:
    """
    メイン関数。
    Webhook URLの取得・バリデーションは get_config() 内で完結しているため、
    main() では設定取得後すぐに処理を開始できる。
    """
    # P0-1: get_config() 内で Secrets Manager から Webhook URL を取得済み
    config = get_config()
    use_teams_post = config["USE_TEAMS_POST"]
    teams_webhook_url = config["TEAMS_WEBHOOK_URL"]

    # AWSアカウントIDを取得
    account_id = get_account_id()
    logger.info(f"AWS Account ID: {account_id}")

    # boto3 CostExplorer クライアントをモック化できるよう必ず get_client() 経由にする
    client = get_client()
    explorer = CostExplorer(client)

    start_date, end_date = get_date_range()
    period = {"Start": start_date, "End": end_date}
    start_day_str = datetime.strptime(start_date, "%Y-%m-%d").strftime("%m/%d")
    # P2: Cost Explorer API の End は「その日の 0:00」を指すため、
    # 表示上は1日引いて「昨日まで（= 実質の集計最終日）」を表示する
    end_day_str = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%m/%d")

    # --- クレジット適用後 ---
    title_after, services_after = handle_cost_report(
        explorer, period, include_credit=True, start_day=start_day_str, end_day=end_day_str
    )
    title_after = f"AWSアカウント {account_id}\n" + title_after
    print_report(title_after, services_after)
    if use_teams_post:
        # P0-3: post_to_teams は bool を返す。False でも Lambda は正常終了とする。
        if not post_to_teams(title_after, services_after, webhook_url=teams_webhook_url):
            logger.warning("クレジット適用後レポートの Teams 通知に失敗しました。処理を継続します。")

    # --- クレジット適用前 ---
    title_before, services_before = handle_cost_report(
        explorer, period, include_credit=False, start_day=start_day_str, end_day=end_day_str
    )
    title_before = f"AWSアカウント {account_id}\n" + title_before
    print_report(title_before, services_before)
    if use_teams_post:
        if not post_to_teams(title_before, services_before, webhook_url=teams_webhook_url):
            logger.warning("クレジット適用前レポートの Teams 通知に失敗しました。処理を継続します。")


def lambda_handler(event: dict, context: Any) -> dict:
    # P2: AWSベストプラクティスに合わせてレスポンスdictを返す
    main()
    return {"statusCode": 200, "body": "Cost report generated successfully."}


if __name__ == "__main__":
    main()