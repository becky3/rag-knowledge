"""site_rules.py のテスト.

仕様: docs/specs/converter.md「サイト別抽出ルール設定ファイル」
計画: aidlc-docs/plan-work/issue-786.md

テスト方針:
- L1 Unit Test。host マッチング・selector 試行順・フォールバック・共通除去合成の各観点を検証する
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from rag.converter.handlers import _clean_content_area, _find_content_area
from rag.converter.site_rules import (
    CompiledSiteRules,
    SiteRulesConfig,
    compile_site_rules,
    extract_web_host,
    load_site_rules,
)


def _make_site_rules(
    *,
    content_id_patterns: list[str] | None = None,
    content_class_patterns: list[str] | None = None,
    remove_class_tokens: list[str] | None = None,
    hosts: dict[str, dict[str, list[str]]] | None = None,
) -> CompiledSiteRules:
    """テスト用に CompiledSiteRules を構築する."""
    raw = SiteRulesConfig.model_validate({
        "default": {
            "content_id_patterns": content_id_patterns or ["main-content", "content"],
            "content_class_patterns": content_class_patterns or ["main-content"],
            "remove_class_tokens": remove_class_tokens or ["sidebar"],
        },
        "hosts": hosts or {},
    })
    return compile_site_rules(raw)


# ============================================================
# extract_web_host
# ============================================================


class TestExtractWebHost:
    """extract_web_host のテスト."""

    def test_web_https_path_returns_host(self) -> None:
        assert extract_web_host("web/https/example.com/foo.html") == "example.com"

    def test_web_http_path_returns_host(self) -> None:
        assert extract_web_host("web/http/example.com/foo.html") == "example.com"

    def test_aozora_path_returns_none(self) -> None:
        assert extract_web_host("aozora/000035/001567.html") is None

    def test_bluesky_path_returns_none(self) -> None:
        assert extract_web_host("bluesky/did:plc:xxx/2026/03/rkey.json") is None

    def test_backslash_path_normalized(self) -> None:
        assert (
            extract_web_host("web\\https\\example.com\\foo.html") == "example.com"
        )

    def test_too_short_path_returns_none(self) -> None:
        assert extract_web_host("web") is None
        assert extract_web_host("web/https") is None

    def test_empty_host_returns_none(self) -> None:
        """host 部分が空文字列のパス（不正形式）は None を返す."""
        assert extract_web_host("web/https//foo.html") is None


# ============================================================
# SiteRulesConfig validation
# ============================================================


class TestSiteRulesConfigValidation:
    """pydantic バリデーションのテスト."""

    def test_default_requires_all_fields(self) -> None:
        with pytest.raises(ValidationError):
            SiteRulesConfig.model_validate({
                "default": {
                    "content_id_patterns": ["main"],
                    # content_class_patterns missing
                    "remove_class_tokens": ["sidebar"],
                },
            })

    def test_default_rejects_empty_pattern_list(self) -> None:
        with pytest.raises(ValidationError):
            SiteRulesConfig.model_validate({
                "default": {
                    "content_id_patterns": [],
                    "content_class_patterns": ["main"],
                    "remove_class_tokens": ["sidebar"],
                },
            })

    def test_unknown_section_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SiteRulesConfig.model_validate({
                "default": {
                    "content_id_patterns": ["main"],
                    "content_class_patterns": ["main"],
                    "remove_class_tokens": ["sidebar"],
                },
                "unknown_section": {"foo": "bar"},
            })

    def test_unknown_host_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SiteRulesConfig.model_validate({
                "default": {
                    "content_id_patterns": ["main"],
                    "content_class_patterns": ["main"],
                    "remove_class_tokens": ["sidebar"],
                },
                "hosts": {
                    "example.com": {
                        "content_selectors": ["#main"],
                        "unknown_field": "x",
                    },
                },
            })


# ============================================================
# load_site_rules
# ============================================================


class TestLoadSiteRules:
    """load_site_rules のテスト."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.toml"
        with pytest.raises(FileNotFoundError):
            load_site_rules(missing)

    def test_valid_toml_loads(self, tmp_path: Path) -> None:
        toml_path = tmp_path / "site_rules.toml"
        toml_path.write_text(
            """
[default]
content_id_patterns = ["main"]
content_class_patterns = ["main"]
remove_class_tokens = ["sidebar"]

[hosts."www.example.com"]
content_selectors = ["#main"]
""",
            encoding="utf-8",
        )
        compiled = load_site_rules(toml_path)
        assert compiled.raw.default.content_id_patterns == ["main"]
        host_rule = compiled.get_host_rule("www.example.com")
        assert host_rule is not None
        assert host_rule.content_selectors == ["#main"]

    def test_invalid_toml_syntax_raises_valueerror_with_path(
        self, tmp_path: Path,
    ) -> None:
        """TOML 構文不正時にファイルパスを含む ValueError を送出する."""
        toml_path = tmp_path / "site_rules.toml"
        toml_path.write_text("not = valid = toml", encoding="utf-8")
        with pytest.raises(ValueError, match=re.escape(str(toml_path))):
            load_site_rules(toml_path)

    def test_validation_error_raises_valueerror_with_path(
        self, tmp_path: Path,
    ) -> None:
        """pydantic バリデーション失敗時にファイルパスを含む ValueError を送出する."""
        toml_path = tmp_path / "site_rules.toml"
        toml_path.write_text(
            """
[default]
content_id_patterns = ["main"]
# content_class_patterns と remove_class_tokens が欠落
""",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=re.escape(str(toml_path))):
            load_site_rules(toml_path)


# ============================================================
# _find_content_area: host rule precedence + fallback
# ============================================================


class TestFindContentArea:
    """_find_content_area の host ルール優先 + 共通フォールバックの統合テスト."""

    def test_host_selector_hits_overrides_common(self) -> None:
        """host の content_selectors にヒットした要素を共通優先順より優先する."""
        html = """
        <html><body>
          <article>本来 article が優先される</article>
          <div id="maintxt"><div class="entry-content">これが真の本文</div></div>
        </body></html>
        """
        rules = _make_site_rules(
            hosts={"www.4gamer.net": {
                "content_selectors": ["#maintxt .entry-content"],
                "remove_selectors": [],
            }},
        )
        soup = BeautifulSoup(html, "html.parser")
        result = _find_content_area(soup, rules, host="www.4gamer.net")
        assert result.get_text(strip=True) == "これが真の本文"

    def test_host_selector_all_miss_falls_back_to_common(self) -> None:
        """host の content_selectors が全ミスした場合、共通優先順にフォールバックする."""
        html = """
        <html><body>
          <article>article fallback</article>
          <div id="other">not used</div>
        </body></html>
        """
        rules = _make_site_rules(
            hosts={"www.4gamer.net": {
                "content_selectors": ["#maintxt"],
                "remove_selectors": [],
            }},
        )
        soup = BeautifulSoup(html, "html.parser")
        result = _find_content_area(soup, rules, host="www.4gamer.net")
        assert result.name == "article"

    def test_host_without_rule_uses_common(self) -> None:
        """ルール未定義ホストは共通優先順のみで判定する."""
        html = """
        <html><body>
          <div class="main-content">common class match</div>
        </body></html>
        """
        rules = _make_site_rules(
            content_class_patterns=["main-content"],
        )
        soup = BeautifulSoup(html, "html.parser")
        result = _find_content_area(soup, rules, host="other.example.com")
        assert "common class match" in result.get_text()

    def test_none_host_uses_common(self) -> None:
        """host=None でも共通優先順で動作する."""
        html = "<html><body><main>main tag</main></body></html>"
        rules = _make_site_rules()
        soup = BeautifulSoup(html, "html.parser")
        result = _find_content_area(soup, rules, host=None)
        assert result.name == "main"

    def test_invalid_selector_skipped(self) -> None:
        """不正な CSS selector はスキップして次の selector を試行する."""
        html = """
        <html><body>
          <article>fallback</article>
          <div id="real-content">real</div>
        </body></html>
        """
        # 1 つ目は不正（":::invalid:::"）、2 つ目は有効
        rules = _make_site_rules(
            hosts={"example.com": {
                "content_selectors": [":::invalid:::", "#real-content"],
                "remove_selectors": [],
            }},
        )
        soup = BeautifulSoup(html, "html.parser")
        result = _find_content_area(soup, rules, host="example.com")
        assert result.get_text(strip=True) == "real"

    def test_host_exact_match_only(self) -> None:
        """完全一致のみ。サブドメイン違いはルール適用されない."""
        html = """
        <html><body>
          <article>common fallback</article>
          <div id="maintxt">site rule target</div>
        </body></html>
        """
        rules = _make_site_rules(
            hosts={"www.4gamer.net": {
                "content_selectors": ["#maintxt"],
                "remove_selectors": [],
            }},
        )
        soup = BeautifulSoup(html, "html.parser")
        # news.4gamer.net は別ホストとして扱われ、site rule が適用されない
        result = _find_content_area(soup, rules, host="news.4gamer.net")
        assert result.name == "article"


# ============================================================
# _clean_content_area: site-specific remove_selectors
# ============================================================


class TestCleanContentArea:
    """_clean_content_area の共通除去 + サイト別追加除去の合成テスト."""

    def test_common_remove_class_tokens_applied(self) -> None:
        html = """
        <div>
          <p>keep me</p>
          <div class="sidebar">remove</div>
        </div>
        """
        rules = _make_site_rules(remove_class_tokens=["sidebar"])
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("div")
        _clean_content_area(content, rules, host=None)
        assert "remove" not in content.get_text()
        assert "keep me" in content.get_text()

    def test_site_remove_selectors_added_to_common(self) -> None:
        """site rule の remove_selectors は共通除去に追加適用される."""
        html = """
        <div>
          <p>keep</p>
          <div class="sidebar">common remove</div>
          <div data-role="ad">site remove</div>
        </div>
        """
        rules = _make_site_rules(
            remove_class_tokens=["sidebar"],
            hosts={"example.com": {
                "content_selectors": [],
                "remove_selectors": ['[data-role="ad"]'],
            }},
        )
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("div")
        _clean_content_area(content, rules, host="example.com")
        text = content.get_text()
        assert "keep" in text
        assert "common remove" not in text
        assert "site remove" not in text

    def test_invalid_remove_selector_skipped(self) -> None:
        """site rule の remove_selectors 内の不正 CSS selector はスキップして続行する."""
        html = """
        <div>
          <p>keep</p>
          <div class="sidebar">common remove</div>
          <div data-role="ad">site remove</div>
        </div>
        """
        # 1 つ目は不正、2 つ目は有効
        rules = _make_site_rules(
            remove_class_tokens=["sidebar"],
            hosts={"example.com": {
                "content_selectors": [],
                "remove_selectors": [":::invalid:::", '[data-role="ad"]'],
            }},
        )
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("div")
        _clean_content_area(content, rules, host="example.com")
        text = content.get_text()
        assert "keep" in text
        assert "common remove" not in text  # 共通除去は適用される
        assert "site remove" not in text  # 有効な selector は適用される

    def test_no_host_rule_only_common_removal(self) -> None:
        """ホストにルールがない場合は共通除去のみ適用される."""
        html = """
        <div>
          <p>keep</p>
          <div class="sidebar">remove</div>
          <div data-role="ad">stays (no host rule)</div>
        </div>
        """
        rules = _make_site_rules(remove_class_tokens=["sidebar"])
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("div")
        _clean_content_area(content, rules, host="unknown.com")
        text = content.get_text()
        assert "keep" in text
        assert "remove" not in text
        assert "stays" in text
