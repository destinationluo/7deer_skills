import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "signallayer_client.py"
SPEC = importlib.util.spec_from_file_location("signallayer_client", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content_type="application/json"):
        self.status_code = status_code
        self.payload = payload if payload is not None else {"success": True}
        self.headers = {"content-type": content_type}
        self.ok = 200 <= status_code < 300

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses=None):
        self.headers = {}
        self.responses = list(responses or [FakeResponse()])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class SignalLayerClientTests(unittest.TestCase):
    def test_environment_key_has_priority(self):
        with patch.dict(os.environ, {"SIGNALLAYER_API_KEY": "sl_from_env"}):
            self.assertEqual(MODULE.load_api_key(), "sl_from_env")

    def test_create_uses_real_contract_and_client_defaults(self):
        response = FakeResponse(
            201,
            {
                "success": True,
                "campaign": {"id": "id", "status": "processing"},
            },
        )
        session = FakeSession([response])
        client = MODULE.SignalLayerClient(
            api_key="sl_test",
            base_url="https://example.test/api/openclaw/",
            session=session,
        )

        client.create_campaign("https://example.com", "Example")

        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://example.test/api/openclaw/create-campaign")
        self.assertEqual(kwargs["json"]["targetUrl"], "https://example.com")
        self.assertEqual(kwargs["json"]["brandName"], "Example")
        self.assertEqual(kwargs["json"]["linkCount"], 200)
        self.assertEqual(kwargs["json"]["speed"], "natural")
        self.assertNotIn("target_url", kwargs["json"])
        self.assertNotIn("quantity", kwargs["json"])

    def test_status_and_list_paths_match_server(self):
        session = FakeSession([FakeResponse(), FakeResponse()])
        client = MODULE.SignalLayerClient(
            api_key="sl_test",
            base_url="https://example.test/api/openclaw",
            session=session,
        )

        client.campaign_status("campaign-id")
        client.campaigns(limit=25, offset=50)

        self.assertTrue(session.calls[0][1].endswith("/campaign-status/campaign-id"))
        self.assertTrue(session.calls[1][1].endswith("/campaigns"))
        self.assertEqual(session.calls[1][2]["params"], {"limit": 25, "offset": 50})

    def test_api_error_includes_status_code_and_stable_code(self):
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    {
                        "success": False,
                        "error": "Invalid speed",
                        "code": "INVALID_REQUEST",
                        "field": "speed",
                    },
                )
            ]
        )
        client = MODULE.SignalLayerClient(api_key="sl_test", session=session)

        with self.assertRaisesRegex(
            MODULE.SignalLayerError,
            r"400 INVALID_REQUEST: Invalid speed \(field: speed\)",
        ):
            client.credits()

    def test_non_json_response_points_to_bad_base_url_or_path(self):
        session = FakeSession(
            [FakeResponse(404, ValueError("html"), content_type="text/html")]
        )
        client = MODULE.SignalLayerClient(api_key="sl_test", session=session)

        with self.assertRaisesRegex(MODULE.SignalLayerError, "non-JSON"):
            client.credits()

    def test_cli_rejects_removed_instant_speed(self):
        parser = MODULE.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--speed", "instant"])


if __name__ == "__main__":
    unittest.main()
