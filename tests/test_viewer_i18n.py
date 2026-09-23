from pathlib import Path

from cfd_twin.mesh_viewer import HOME_HTML, OFFLINE_JAVASCRIPT
from cfd_twin.viewer import HTML, JAVASCRIPT, refresh_viewer_ui_assets


def test_offline_home_keeps_language_switch_and_local_assets() -> None:
    assert 'data-language="en"' in HOME_HTML
    assert 'src="i18n.js?v=ko-en-7"' in HOME_HTML
    assert 'src="offline-app.js?v=demo-workflow-1"' in HOME_HTML
    assert 'href="portal.css?v=button-text-1"' in HOME_HTML
    assert 'src="/app.js' not in HOME_HTML
    assert 'id="landing-view"' in HOME_HTML
    assert 'id="go-start"' in HOME_HTML
    assert 'id="go-graphics"' in HOME_HTML
    assert 'id="menu-view"' not in HOME_HTML
    assert '$("go-start").onclick=()=>show("calculate")' in OFFLINE_JAVASCRIPT
    assert '$("go-graphics").onclick=()=>show("results")' in OFFLINE_JAVASCRIPT
    assert 'Start New CFD Calculation' in OFFLINE_JAVASCRIPT
    assert 'Demo CAD ready' not in OFFLINE_JAVASCRIPT
    assert 'No actual solver run' not in OFFLINE_JAVASCRIPT
    assert '파일로 연 화면에서는 새 계산을 시작할 수 없습니다' not in OFFLINE_JAVASCRIPT


def test_portable_language_links_respect_top_level_base_href() -> None:
    i18n = (Path(__file__).parents[1] / "src" / "cfd_twin" / "workflow_assets" / "i18n.js").read_text(encoding="utf-8")
    assert "new URL(href,document.baseURI || location.href)" in i18n
    assert "new URL(href,location.href)" not in i18n


def test_results_graphs_redraw_in_selected_language() -> None:
    assert 'data-language="en"' in HTML
    assert 'src="i18n.js"' in HTML
    assert 'context.fillText(tr("시간 [s]")' in JAVASCRIPT
    assert 'context.fillText(tr(`${convergence.scope} 수렴 이력`)' in JAVASCRIPT
    assert 'cfdtwin:languagechange' in JAVASCRIPT
    assert "단계 잔차 목표 미달" not in JAVASCRIPT
    assert 'id="show-temperature-legend"' in HTML
    assert 'id="show-velocity-legend"' in HTML
    assert 'id="show-legend"' not in HTML
    assert 'id="show-port-callouts"' in HTML
    assert 'id="show-extrema-callouts"' in HTML
    assert 'id="reset-overlays"' in HTML
    assert 'overlayStorageKey="yuFluidViewerOverlayPositionsV3"' in JAVASCRIPT
    assert '선택 · 3D 모델에서 잠시 강조됩니다' in JAVASCRIPT


def test_english_dynamic_ui_phrases_are_covered() -> None:
    i18n = (Path(__file__).parents[1] / "src" / "cfd_twin" / "workflow_assets" / "i18n.js").read_text(encoding="utf-8")
    for korean, english in {
        "탈이온수": "Deionized Water",
        "목표 메시": "Target Mesh",
        "열원": "Heat Source",
        "배열 고정": "Fixed Arrangement",
        "형상 확인 대기": "Awaiting Geometry Review",
        "탱크·포트 외형": "Tank and Ports",
        "냉각수 유체 영역": "Coolant Fluid Region",
        "23개 배터리 고체 영역": "23 Battery Solid Regions",
        "선택 · 3D 모델에서 잠시 강조됩니다": "selected · briefly highlighted in the 3D model",
    }.items():
        assert f'"{korean}":"{english}"' in i18n
    assert "#flow-sampling,#current-mode,.canvas-note { display:none !important; }" in (Path(__file__).parents[1] / "src" / "cfd_twin" / "viewer.py").read_text(encoding="utf-8")


def test_refresh_ui_keeps_solver_results_and_model(tmp_path: Path) -> None:
    (tmp_path / "results.html").write_text('<script src="model.abc123.js"></script>', encoding="utf-8")
    (tmp_path / "model.js").write_text("ORIGINAL MODEL", encoding="utf-8")
    (tmp_path / "model.abc123.js").write_text("ORIGINAL HASHED MODEL", encoding="utf-8")
    (tmp_path / "mesh-volume-data.js").write_text("ORIGINAL MESH", encoding="utf-8")
    (tmp_path / "cad").mkdir()

    refresh_viewer_ui_assets(tmp_path)

    assert (tmp_path / "model.js").read_text(encoding="utf-8") == "ORIGINAL MODEL"
    assert (tmp_path / "model.abc123.js").read_text(encoding="utf-8") == "ORIGINAL HASHED MODEL"
    assert (tmp_path / "mesh-volume-data.js").read_text(encoding="utf-8") == "ORIGINAL MESH"
    assert 'src="model.abc123.js"' in (tmp_path / "results.html").read_text(encoding="utf-8")
    assert (tmp_path / "i18n.js").is_file()
    assert "Top View" in (tmp_path / "cad" / "dimensioned_top.en.svg").read_text(encoding="utf-8")
    assert "Side View" in (tmp_path / "cad" / "dimensioned_side.en.svg").read_text(encoding="utf-8")


def test_refresh_distribution_adds_cad_drawings_without_solver_regeneration(tmp_path: Path) -> None:
    viewer = tmp_path / "viewer"
    viewer.mkdir()
    (tmp_path / "distribution_identity.json").write_text("{}", encoding="utf-8")
    (viewer / "results.html").write_text('<script src="model.js"></script>', encoding="utf-8")
    (viewer / "model.js").write_text("ORIGINAL MODEL", encoding="utf-8")

    refresh_viewer_ui_assets(viewer)

    assert (viewer / "cad" / "dimensioned_top.en.svg").is_file()
    assert 'href="cad/index.html"' in (viewer / "results.html").read_text(encoding="utf-8")
    assert '<base href="viewer/">' in (tmp_path / "index.html").read_text(encoding="utf-8")
    assert (viewer / "model.js").read_text(encoding="utf-8") == "ORIGINAL MODEL"
