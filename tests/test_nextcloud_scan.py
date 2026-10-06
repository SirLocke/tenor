from pathlib import Path
from unittest.mock import patch

from src.pipeline import scan_nextcloud


def test_scan_nextcloud_disabled():
    cfg = {"nextcloud": {"enabled": False, "scan_cmd": "echo {output_dir}"}}
    with patch("subprocess.run") as mock_run:
        scan_nextcloud(cfg, Path("/tmp/output"))
        mock_run.assert_not_called()


def test_scan_nextcloud_normal():
    cfg = {
        "nextcloud": {
            "enabled": True,
            "scan_cmd": "docker exec nextcloud php occ files:scan --path={output_dir}",
            "timeout": 45,
        }
    }
    output_dir = Path("/nas/video-cut/output")
    with patch("subprocess.run") as mock_run:
        scan_nextcloud(cfg, output_dir)
        mock_run.assert_called_once_with(
            [
                "docker",
                "exec",
                "nextcloud",
                "php",
                "occ",
                "files:scan",
                "--path=/nas/video-cut/output",
            ],
            shell=False,
            timeout=45,
            check=False,
            capture_output=True,
        )


def test_scan_nextcloud_command_injection_prevention():
    cfg = {
        "nextcloud": {
            "enabled": True,
            "scan_cmd": "docker exec nextcloud php occ files:scan --path={output_dir}",
            "timeout": 30,
        }
    }
    # Attempt command injection via output_dir
    malicious_dir = Path("/tmp/output; touch /tmp/injected; $(id)")
    with patch("subprocess.run") as mock_run:
        scan_nextcloud(cfg, malicious_dir)
        # Verify subprocess.run was called with shell=False and the malicious path treated as a single argument string token
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert kwargs["shell"] is False
        cmd_args = args[0]
        assert cmd_args[-1] == "--path=/tmp/output; touch /tmp/injected; $(id)"
