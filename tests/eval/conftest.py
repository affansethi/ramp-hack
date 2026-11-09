"""Eval terminal reporting: accuracy/precision stay unsigned without goldens."""

from test_replay_eval import eval_terminal_line


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Emit the unsigned replay summary in pytest's terminal summary."""
    del exitstatus, config
    nodeids = {
        str(getattr(report, "nodeid", ""))
        for reports in terminalreporter.stats.values()
        for report in reports
    }
    checked = sum(1 for nodeid in nodeids if nodeid.startswith("tests/eval/"))
    terminalreporter.write_line(eval_terminal_line(checked=checked))
