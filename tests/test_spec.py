import pytest

from viper_pm.spec import AppSpec, load_config, parse_memory


def test_parse_memory():
    assert parse_memory("300M") == 300 * 1024**2
    assert parse_memory("1.5G") == int(1.5 * 1024**3)
    assert parse_memory("512K") == 512 * 1024
    assert parse_memory("1024") == 1024
    assert parse_memory(2048) == 2048
    assert parse_memory(None) is None
    with pytest.raises(ValueError):
        parse_memory("lots")


def test_spec_validation():
    with pytest.raises(ValueError):
        AppSpec.from_dict({"name": "bad name!", "cmd": "sleep 1"})
    with pytest.raises(ValueError):
        AppSpec.from_dict({"name": "ok", "cmd": ""})
    with pytest.raises(ValueError):
        AppSpec.from_dict({"name": "ok", "cmd": "sleep 1", "workers": 0})
    with pytest.raises(ValueError):
        AppSpec.from_dict({"name": "ok", "cmd": "sleep 1", "nonsense": True})
    spec = AppSpec.from_dict(
        {"name": "ok", "cmd": "sleep 1", "max_memory": "100M", "instances": 3}
    )
    assert spec.max_memory == 100 * 1024**2
    assert spec.workers == 3  # 'instances' alias


def test_load_config(tmp_path):
    cfg = tmp_path / "viper.yml"
    cfg.write_text(
        "apps:\n"
        "  - name: web\n"
        "    cmd: python3 -m http.server 8000\n"
        "    workers: 2\n"
        "    interpreter: auto\n"
        "  - name: worker\n"
        "    cmd: python3 worker.py\n"
        "    cwd: sub\n"
    )
    specs = load_config(str(cfg))
    assert [s.name for s in specs] == ["web", "worker"]
    assert specs[0].cwd == str(tmp_path)          # default cwd = config dir
    assert specs[1].cwd == str(tmp_path / "sub")  # relative cwd resolved
    assert specs[0].venv == "auto"                # 'interpreter' alias


def test_load_config_duplicate_names(tmp_path):
    cfg = tmp_path / "viper.yml"
    cfg.write_text(
        "apps:\n"
        "  - {name: a, cmd: sleep 1}\n"
        "  - {name: a, cmd: sleep 2}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_config(str(cfg))
