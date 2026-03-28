from unittest.mock import patch

import app


def test_lambda_handler_calls_main() -> None:
    with patch.object(app, "main") as mock_main:
        result = app.lambda_handler({"source": "aws.events"}, object())
        assert result == {"statusCode": 200, "body": "Cost report generated successfully."}
        mock_main.assert_called_once_with()
