"""K-065: a Python script that takes --host/--port gets them, so the preview waits on the port it really opens."""
import tempfile
import unittest
from pathlib import Path

from app import preview


class FlagsTest(unittest.TestCase):
    def detect(self, source):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'app.py').write_text(source)
            return preview.detect(d, port=8123)['argv'][1:]

    def test_flags_follow_the_script(self):
        self.assertEqual(self.detect('p.add_argument("--host")\np.add_argument("--port")\n'),
                         ['app.py', '--host', '127.0.0.1', '--port', '8123'])
        self.assertEqual(self.detect("p.add_argument('--port', type=int)\n"), ['app.py', '--port', '8123'])
        self.assertEqual(self.detect('import os\nPORT = int(os.environ["PORT"])\n'), ['app.py'])   # PORT env only
        self.assertEqual(self.detect('# --portable mode\n'), ['app.py'])
        self.assertEqual(self.detect('def broken(:\n'), ['app.py'])   # unparsable: no flags   # a quoted flag only, not any mention
        self.assertEqual(self.detect('# p.add_argument("--port")\nHELP = \'p.add_argument("--port")\'\n'), ['app.py'])   # comment, string
        self.assertEqual(self.detect('@click.option("--port", default=1)\ndef main(port):\n    pass\n'), ['app.py', '--port', '8123'])
        self.assertEqual(self.detect('p.add_argument(\n    "--port",\n    type=int,\n)\n'), ['app.py', '--port', '8123'])


if __name__ == '__main__':
    unittest.main()
