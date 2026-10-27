"""Executes every Streamlit step of app.py against a stand-in `streamlit` module.

This catches NameErrors, wrong attribute names and crashes in the UI code paths. It is NOT a
visual test - run `streamlit run app.py` once to look at the real interface.
"""
import glob
import importlib.util
import os
import sys
import types
from types import SimpleNamespace

from helpers import csv_bytes  # noqa: F401  (sets sys.path)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SS(dict):
    __getattr__ = dict.get

    def __setattr__(self, k, v):
        self[k] = v


class Ctx:
    def __init__(self, fake):
        self._f = fake

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return getattr(self._f, name)


class Fake(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = SS()
        self.pressed = set()
        self.column_config = SimpleNamespace(SelectboxColumn=lambda **k: None, TextColumn=lambda **k: None)
        self.sidebar = Ctx(self)
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def f(*a, **k):
            self.calls.append(name)
            return None
        return f

    def columns(self, spec, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [Ctx(self) for _ in range(n)]

    def tabs(self, labels):
        return [Ctx(self) for _ in labels]

    def expander(self, *a, **k):
        return Ctx(self)

    container = spinner = expander

    def button(self, label, *a, **k):
        return label in self.pressed

    def file_uploader(self, *a, **k):
        return []

    def checkbox(self, label, value=False, **k):
        return value

    def text_input(self, label, value="", **k):
        return value

    def text_area(self, label, value="", **k):
        return value

    def selectbox(self, label, options, index=0, **k):
        options = list(options)
        return options[index] if options else None

    def radio(self, label, options, index=0, **k):
        return list(options)[index]

    def multiselect(self, label, options, default=None, **k):
        return list(default or [])

    def data_editor(self, df, **k):
        return df


def run_app(fake):
    comp = types.ModuleType("streamlit.components")
    v1 = types.ModuleType("streamlit.components.v1")
    v1.html = lambda *a, **k: None
    comp.v1 = v1
    saved = {k: sys.modules.get(k) for k in ("streamlit", "streamlit.components", "streamlit.components.v1")}
    sys.modules.update({"streamlit": fake, "streamlit.components": comp, "streamlit.components.v1": v1})
    fake.components = comp
    try:
        spec = importlib.util.spec_from_file_location("app_under_test", os.path.join(HERE, "app.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def prepared(mode="direct"):
    from modules import workflow as wf
    fake = Fake()
    files = [(os.path.basename(p), open(p, "rb").read()) for p in sorted(glob.glob(os.path.join(HERE, "examples", "*")))]
    ds, reps = wf.load(files)
    ss = fake.session_state
    ss.update(step=1, max_step=8, files=files, file_sig="x", datasets=ds, reports=reps, has_header=True, analysis=None,
              dialect="postgresql", mode=mode, staging=False, dirty=True, outputs=None, profiles=None)
    return fake


def test_every_step_renders_without_crashing():
    fake = prepared()
    for step in (1, 2, 3):
        fake.session_state.step = step
        run_app(fake)
    fake.pressed = {"Analyse and propose a schema"}
    fake.session_state.step = 3
    run_app(fake)
    assert fake.session_state.analysis is not None and fake.session_state.step == 4
    fake.pressed = set()
    for step in (4, 5, 6, 7, 8):
        fake.session_state.step = step
        run_app(fake)
    assert "download_button" in fake.calls and "code" in fake.calls


def test_relational_mode_and_actions_render():
    fake = prepared("relational")
    fake.pressed = {"Analyse and propose a schema"}
    fake.session_state.step = 3
    run_app(fake)
    fake.pressed = {"Reject this foreign key", "Use a generated surrogate key instead", "Apply this date order",
                    "Store this column as text (safe) and approve", "Re-run validation"}
    for step in (4, 5):
        fake.session_state.step = step
        run_app(fake)


def test_steps_before_analysis_ask_for_it():
    fake = prepared()
    for step in (4, 5, 6, 7, 8):
        fake.session_state.step = step
        run_app(fake)
    assert fake.session_state.analysis is None
