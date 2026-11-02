"""PM's certificate step must not use /tmp: on Jibo it is a small tmpfs that is often full,
and curl then fails with "(23) Failed writing body". Runs each script's step with a stand-in curl."""
import glob, os, subprocess, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def certificate_step(script):
    lines = open(script).read().splitlines()
    start = next(i for i, l in enumerate(lines) if 'Downloading and installing ISRG Root X1' in l)
    end = next(i for i in range(start, len(lines)) if lines[i].startswith('ln -sf'))
    return '\n'.join(l for l in lines[start:end] if not l.lstrip().startswith('#'))


class CertificateStep(unittest.TestCase):
    def test_downloads_next_to_destination(self):
        scripts = sorted(glob.glob(os.path.join(ROOT, 'PM', '*.sh')))
        self.assertEqual(len(scripts), 4)
        for script in scripts:
            with self.subTest(script=os.path.basename(script)), tempfile.TemporaryDirectory() as work:
                step = certificate_step(script)
                self.assertNotIn('/tmp', step)
                bin_dir = os.path.join(work, 'bin'); os.mkdir(bin_dir)
                with open(os.path.join(bin_dir, 'curl'), 'w') as curl:  # writes a PEM to the -o path
                    curl.write('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\n'
                               'printf -- "-----BEGIN CERTIFICATE-----\\nX\\n-----END CERTIFICATE-----\\n" > "$2"\n')
                os.chmod(os.path.join(bin_dir, 'curl'), 0o755)
                ca = os.path.join(work, 'opt', 'openjibo-ca.crt'); os.mkdir(os.path.dirname(ca))
                env = dict(os.environ, PATH=bin_dir + ':' + os.environ['PATH'], CA=ca, CA_URL='https://example/x.pem')
                subprocess.run(['sh', '-e', '-c', step], env=env, check=True)
                self.assertIn('BEGIN CERTIFICATE', open(ca).read())
                self.assertEqual(oct(os.stat(ca).st_mode & 0o777), '0o644')
                self.assertFalse(os.path.exists(ca + '.new'))


if __name__ == '__main__':
    unittest.main()
