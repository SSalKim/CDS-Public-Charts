from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("git"),"Git is required for the sparse checkout regression")
class HelperCheckoutTests(unittest.TestCase):
    def test_explicit_helper_patterns_restore_previously_skipped_entry_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git","-c",f"safe.directory={root.as_posix()}","-c","user.name=CheckoutProbe",
                     "-c","user.email=probe@example.invalid","-c","commit.gpgsign=false",*args],
                    cwd=root,text=True,encoding="utf-8",stderr=subprocess.STDOUT)

            git("init")
            files = ("public_charts_action.py","public_charts_helper.py","public_charts_runtime.py")
            for filename in files:
                (root/filename).write_text("pass\n",encoding="utf-8")
            (root/"public_charts").mkdir()
            (root/"public_charts/__init__.py").write_text("",encoding="utf-8")
            git("add",".")
            git("commit","-m","Fixture")
            git("sparse-checkout","set","--no-cone","/public_charts_runtime.py","/public_charts/")
            self.assertFalse((root/files[0]).exists())
            self.assertIn("S public_charts_action.py",git("ls-files","-v","--",*files))
            git("sparse-checkout","set","--no-cone",*("/"+name for name in files),"/public_charts/")
            self.assertTrue(all((root/name).is_file() for name in files))
            self.assertNotIn("S ",git("ls-files","-v","--",*files))


if __name__ == "__main__":
    unittest.main()
