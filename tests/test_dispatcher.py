"""The master dispatcher lists every tool and refuses unknown names."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mastercli.cli import main


class DispatcherTests(unittest.TestCase):
    def test_help_names_every_tool(self):
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["--help"])
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        for name in ("gpumaster", "macmaster", "thermalmaster", "tokenmaster"):
            self.assertIn(name, text)

    def test_unknown_tool(self):
        from io import StringIO
        from contextlib import redirect_stderr
        buf = StringIO()
        with redirect_stderr(buf):
            rc = main(["no-such-tool"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown tool", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
