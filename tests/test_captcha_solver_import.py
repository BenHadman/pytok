import sys
import types


def test_captcha_solver_defers_opencv_import(monkeypatch):
    from pytok import captcha_solver

    fake_cv2 = types.SimpleNamespace(marker="opencv")
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    assert captcha_solver._opencv() is fake_cv2
