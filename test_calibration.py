"""Tests for the self-tuning / calibration plumbing (no network)."""
import server
import ev_model


def test_feature_vec_matches_feature_names():
    v = server._fill_feature_vec(1, 2, 3, 0.5, 0.6, 4, 5)
    assert len(v) == len(server.FILL_FEATURES)


def test_predict_fill_prob_none_model_is_none():
    assert server.predict_fill_prob(None, [0] * 7) is None


def test_apply_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CALIBRATION_PATH", tmp_path / "calibration.json")
    monkeypatch.setattr(server, "_calibration_cache", None)
    assert server.ev_scale() == 1.0
    server.apply_calibration({"ev_scale": 0.5, "params": {"dip_z": -1.6, "capture_share": 0.1}})
    assert server.ev_scale() == 0.5
    assert server.formula_params()["dip_z"] == -1.6
    assert server.formula_params()["capture_share"] == 0.1
    # unknown params are ignored, defaults preserved
    server.apply_calibration({"params": {"bogus": 9}})
    assert "bogus" not in server.formula_params()
    assert server.formula_params()["overheated_z"] == 2.0
    # reset restores defaults
    server.apply_calibration({"reset": True})
    assert server.ev_scale() == 1.0
    assert server.formula_params()["dip_z"] == -1.2


def test_fill_model_roundtrip_via_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CALIBRATION_PATH", tmp_path / "calibration.json")
    monkeypatch.setattr(server, "_calibration_cache", None)
    rows = [[i, 0, 0, 0, 0, 0, 0] for i in range(-10, 10)]
    labels = [1 if r[0] > 0 else 0 for r in rows]
    model = ev_model.train_logistic(rows, labels, feature_names=server.FILL_FEATURES)
    server.apply_calibration({"fill_model": model})
    pf = server.predict_fill_prob(server.load_calibration().get("fill_model"), [5, 0, 0, 0, 0, 0, 0])
    assert pf is not None and 0.0 <= pf <= 1.0
