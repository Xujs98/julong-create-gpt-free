from pathlib import Path

from PIL import Image

from webui.app import create_app


ROOT = Path(__file__).parents[1]
MODERN = ROOT / "webui" / "templates" / "index.html"
LOGIN = ROOT / "webui" / "templates" / "login.html"
LOGO = ROOT / "webui" / "static" / "brand" / "julong-logo.png"


def test_transparent_brand_assets_exist():
    with Image.open(LOGO) as logo:
        assert logo.mode == "RGBA"
        assert logo.size == (512, 512)
        alpha = logo.getchannel("A")
        assert all(alpha.getpixel(point) == 0 for point in ((0, 0), (511, 0), (0, 511), (511, 511)))
        assert alpha.getbbox() is not None

    assert (LOGO.parent / "julong-favicon.png").is_file()
    assert (LOGO.parent / "julong-favicon.ico").is_file()


def test_pages_use_julong_branding_and_favicon():
    modern = MODERN.read_text(encoding="utf-8")
    login = LOGIN.read_text(encoding="utf-8")

    assert "矩龙 搞号" in modern
    assert "矩龙 搞号" in login
    assert 'src="/static/brand/julong-logo.png"' in modern
    assert 'src="/static/brand/julong-logo.png"' in login
    assert all("/static/brand/julong-favicon.png" in source for source in (modern, login))
    assert "GPT Registrator" not in modern
    assert "GPT Registrator" not in login


def test_favicon_is_served_without_login():
    client = create_app(auth_code="brand-test").test_client()
    response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.mimetype in {"image/vnd.microsoft.icon", "image/x-icon"}
    assert len(response.data) > 100
