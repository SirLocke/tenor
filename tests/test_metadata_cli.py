import json
from pathlib import Path
from unittest.mock import patch

from src.metadata import main


def test_main_json_output(tmp_path: Path, capsys):
    # Erstelle ein temporäres Verzeichnis mit einer leeren Fake-Videodatei
    test_file = tmp_path / "test.mp4"
    test_file.touch()

    # Wir mocken config, probe_clip und geocoder um die Pipeline kurz zu halten
    with (
        patch("src.metadata.load_config") as mock_cfg,
        patch("src.metadata.probe_clip") as mock_probe,
        patch("src.metadata.Geocoder"),
    ):
        mock_cfg.return_value = {}

        # Ein Fake ClipInfo, so wie es probe_clip zurückgeben würde
        from src.metadata import ClipInfo

        fake_clip = ClipInfo(path=test_file, duration=5.0)
        mock_probe.return_value = fake_clip

        # Aufruf des main-Moduls mit --json
        args = ["-i", str(tmp_path), "--json"]
        ret = main(args)

        assert ret == 0

        captured = capsys.readouterr()
        # Prüfen, dass der Output auf stdout landet und gültiges JSON ist
        output_data = json.loads(captured.out)

        # Prüfe ob die JSON-Struktur mindestens unsere Test-Datei enthält
        assert len(output_data) == 1
        assert output_data[0]["duration"] == 5.0
        assert output_data[0]["path"] == str(test_file)


def test_main_text_output(tmp_path: Path, capsys):
    test_file = tmp_path / "test2.mp4"
    test_file.touch()

    with (
        patch("src.metadata.load_config") as mock_cfg,
        patch("src.metadata.probe_clip") as mock_probe,
        patch("src.metadata.Geocoder"),
    ):
        mock_cfg.return_value = {}

        from src.metadata import ClipInfo

        fake_clip = ClipInfo(path=test_file, duration=10.5)
        mock_probe.return_value = fake_clip

        args = ["-i", str(tmp_path)]
        ret = main(args)

        assert ret == 0

        captured = capsys.readouterr()
        assert "test2.mp4" in captured.out
        assert "10.5s" in captured.out
