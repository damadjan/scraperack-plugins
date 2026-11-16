import importlib.util
import unittest
from pathlib import Path

GUEST = Path(__file__).resolve().parents[1] / "plugin" / "guest" / "executor.py"

try:
    import pwd  # noqa: F401  (the executor runs on Linux)
except ImportError:
    pwd = None


@unittest.skipIf(pwd is None, "the guest executor needs a POSIX system")
class VncPasswordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("executor", GUEST)
        cls.executor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.executor)

    def test_des(self):
        # FIPS 81-style known answer: key 133457799BBCDFF1, block 0123456789ABCDEF.
        result = self.executor._des_encrypt(bytes.fromhex("133457799BBCDFF1"),
                                            bytes.fromhex("0123456789ABCDEF"))
        self.assertEqual(result.hex(), "85e813540f0ab405")

    def test_matches_vncpasswd(self):
        # Expected values from TigerVNC's `vncpasswd -f`.
        for password, expected in (("abc12345", "f54f4c7a814b4b4b"), ("Zx9", "b3375e568dc0a0c6"),
                                   ("Qw3rTy7u", "1d40cd7b65b17106"),
                                   ("longerthan8", "e520dd266ad75a93"),
                                   ("ygGMCYVK", "d465b830d18eb8e8")):
            self.assertEqual(self.executor.vnc_obfuscate(password).hex(), expected, password)


if __name__ == "__main__":
    unittest.main()
