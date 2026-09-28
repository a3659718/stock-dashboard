"""Offline regressions for the dashboard Telegram path; never sends messages."""
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_functions(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8-sig"))
    selected = [node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == len(names)
    exec(compile(ast.Module(body=selected, type_ignores=[]), filename, "exec"), namespace)


class TelegramRegressionTests(unittest.TestCase):
    def setUp(self):
        self.ns = {"Optional": __import__("typing").Optional,
                   "SEND_OK_COUNT": 0,
                   "_tpe_now_str": lambda: "12:00",
                   "_record_push_safe": Mock(),
                   "_send_message_inner": Mock(return_value=(True, "sent"))}
        load_functions("notifier.py", {"_u16_len", "_split_tg_msg", "send_message"}, self.ns)

    def test_emoji_long_message_terminates_and_preserves_content(self):
        text = "🎯" * 2500
        chunks = self.ns["_split_tg_msg"](text, 3800)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(self.ns["_u16_len"](c) <= 3800 for c in chunks))
        self.assertTrue(self.ns["send_message"](text)[0])
        self.assertEqual(self.ns["_send_message_inner"].call_count, 2)

    def test_whitespace_preserved_and_leading_newline_progresses(self):
        text = "\n" + "漢" * 5000 + "\n   尾端"
        self.assertEqual("".join(self.ns["_split_tg_msg"](text)), text)

    def test_empty_text_never_sends_timestamp(self):
        for text in (None, "", "  \n"):
            self.assertFalse(self.ns["send_message"](text)[0])
        self.ns["_send_message_inner"].assert_not_called()

    def test_cap_does_not_send_uncategorized_tail(self):
        cap = types.ModuleType("push_cap")
        cap.would_allow = Mock(return_value=False)
        with patch.dict("sys.modules", {"push_cap": cap}):
            ok, info = self.ns["send_message"]("股" * 9000, category="test")
        self.assertFalse(ok)
        self.assertIn("daily_cap_skip", info)
        self.ns["_send_message_inner"].assert_not_called()

    def test_precursor_eight_results_send(self):
        ns = {"List": list, "Dict": dict}
        load_functions("limit_up_precursor.py", {"fmt_limit_up_precursor_msg"}, ns)
        picks = [{"stock_id": str(2300 + i), "name": "A&B <測試>",
                  "theme": "題材", "reasons": ["🎯吸籌" * 40] * 4,
                  "warnings": [], "current": 100, "score": 40}
                 for i in range(8)]
        msg = ns["fmt_limit_up_precursor_msg"](picks)
        self.assertIn("A&amp;B &lt;測試&gt;", msg)
        self.assertTrue(self.ns["send_message"](msg)[0])
        sent = "".join(c.args[0] for c in self.ns["_send_message_inner"].call_args_list)
        for pick in picks:
            self.assertIn(pick["stock_id"], sent)

    def test_top_button_routes_existing_precursor_results(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.Name) and n.test.id == "send_lup_top")
        module = types.ModuleType("limit_up_precursor")
        module.fmt_limit_up_precursor_msg = Mock(return_value="precursor message")
        picks = [{"stock_id": "2330"}]
        send = Mock()
        with patch.dict("sys.modules", {"limit_up_precursor": module}):
            exec(compile(ast.Module(body=[branch], type_ignores=[]), "app.py", "exec"),
                 {"send_lup_top": True, "st": types.SimpleNamespace(
                     session_state={"limit_up_precursor": picks}), "_send_tg": send})
        module.fmt_limit_up_precursor_msg.assert_called_once_with(picks)
        send.assert_called_once_with("precursor message", "漲停前兆")

    def test_html_fallback_keeps_silent_delivery(self):
        requests = types.SimpleNamespace(Response=object, post=Mock(side_effect=[
            types.SimpleNamespace(status_code=400, text="can't parse entities"),
            types.SimpleNamespace(status_code=200, text="ok")]))
        ns = {"Optional": __import__("typing").Optional, "requests": requests,
              "_bot_token": lambda: "fake", "_chat_id": lambda: "fake"}
        load_functions("notifier.py", {"_send_message_inner", "_looks_like_html_parse_error"}, ns)
        self.assertTrue(ns["_send_message_inner"]("<b>test", disable_notification=True)[0])
        self.assertTrue(requests.post.call_args.kwargs["data"]["disable_notification"])


if __name__ == "__main__":
    unittest.main()
