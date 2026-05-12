"""HTML サイト別抽出ルール (site_rules.toml) のローダーとデータモデル.

仕様: docs/specs/converter.md「サイト別抽出ルール設定ファイル」

site_rules.toml が SSoT。共通フォールバック用パターン群（[default] セクション）と
ホスト単位の抽出ルール（[hosts."<host>"] セクション）を pydantic で検証し、
正規表現の事前コンパイル済みインスタンスを converter に渡す。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DefaultRules(BaseModel):
    """共通フォールバックで使用するパターン群."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id_patterns: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
    )
    content_class_patterns: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
    )
    remove_class_tokens: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1,
    )


class HostRule(BaseModel):
    """ホスト単位の抽出ルール."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_selectors: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list,
    )
    remove_selectors: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list,
    )


class SiteRulesConfig(BaseModel):
    """site_rules.toml の構造."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: DefaultRules
    hosts: dict[str, HostRule] = Field(default_factory=dict)


@dataclass(frozen=True)
class CompiledSiteRules:
    """事前コンパイル済みのサイトルール.

    converter から繰り返し参照されるため、正規表現は構築時に 1 度だけコンパイルする。
    """

    raw: SiteRulesConfig
    default_id_patterns: tuple[re.Pattern[str], ...]
    default_class_patterns: tuple[re.Pattern[str], ...]
    default_remove_class_re: re.Pattern[str]

    def get_host_rule(self, host: str | None) -> HostRule | None:
        """ホスト名に完全一致するルールを返す.

        Args:
            host: ホスト名（None なら None を返す）

        Returns:
            HostRule または None（ホストにルールが定義されていない場合）
        """
        if host is None:
            return None
        return self.raw.hosts.get(host)


def compile_site_rules(raw: SiteRulesConfig) -> CompiledSiteRules:
    """SiteRulesConfig から CompiledSiteRules を構築する."""
    id_patterns = tuple(
        re.compile(re.escape(p), re.IGNORECASE)
        for p in raw.default.content_id_patterns
    )
    class_patterns = tuple(
        re.compile(re.escape(p), re.IGNORECASE)
        for p in raw.default.content_class_patterns
    )
    remove_re = _compile_remove_class_re(raw.default.remove_class_tokens)
    return CompiledSiteRules(
        raw=raw,
        default_id_patterns=id_patterns,
        default_class_patterns=class_patterns,
        default_remove_class_re=remove_re,
    )


def _compile_remove_class_re(tokens: list[str]) -> re.Pattern[str]:
    """class トークン除去用の正規表現をコンパイルする.

    BS4 は各クラストークンに regex.search() するため ^...$ で完全一致にする。
    """
    return re.compile(
        "^(?:" + "|".join(re.escape(t) for t in tokens) + ")$",
        re.IGNORECASE,
    )


def load_site_rules(path: Path) -> CompiledSiteRules:
    """site_rules.toml を読み込んで CompiledSiteRules を返す.

    Args:
        path: site_rules.toml の絶対パス

    Raises:
        FileNotFoundError: ファイルが存在しない場合
        ValueError: TOML 構文エラー or pydantic バリデーションに失敗した場合
            （ファイルパス・原因を含むメッセージを送出）
    """
    if not path.exists():
        msg = f"site_rules.toml が見つかりません: {path}"
        raise FileNotFoundError(msg)
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            msg = f"site_rules.toml の TOML 解析に失敗しました ({path}): {e}"
            raise ValueError(msg) from e
    try:
        raw = SiteRulesConfig.model_validate(data)
    except ValidationError as e:
        msg = f"site_rules.toml の検証に失敗しました ({path}): {e}"
        raise ValueError(msg) from e
    return compile_site_rules(raw)


def extract_web_host(file_path: str) -> str | None:
    """source_store の相対パスから web の host 部分を抽出する.

    web/{scheme}/{host}/... 形式（scheme は https / http）のパスのみ host を返す。
    aozora / bluesky / youtube 等の web 以外のパスは None を返す。
    host 部分が空文字列の場合（不正パス）も None を返す。

    Args:
        file_path: source_store ルート基準の相対パス（POSIX / Windows 両形式可）

    Returns:
        host 文字列、または web パスでなければ None
    """
    parts = file_path.replace("\\", "/").split("/")
    if (
        len(parts) >= 3
        and parts[0] == "web"
        and parts[1] in ("https", "http")
        and parts[2]
    ):
        return parts[2]
    return None
