"""ローカルファイルインジェスター

仕様: docs/specs/rag-knowledge.md (LocalFileIngester セクション)
Issue: #98
"""

from __future__ import annotations

import glob as glob_module
import logging
from pathlib import Path

from .base import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

# 対応拡張子
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt"})


class LocalFileIngester(BaseIngester):
    """ローカルファイル取り込み用インジェスター.

    Markdown・テキスト等のファイル読み込みに対応し、
    ディレクトリ一括取り込み（discover + glob パターン）をサポートする。

    セキュリティ:
    - 許可ディレクトリによるアクセス制限（パストラバーサル防止）
    - 未設定時はフェイルクローズ（取り込み拒否）
    - シンボリックリンク解決後のパスチェック
    """

    def __init__(self, *, allowed_dirs: list[str] | None = None) -> None:
        """LocalFileIngester を初期化する.

        Args:
            allowed_dirs: 許可ディレクトリのリスト。
                None または空リストの場合はファイル取り込みを拒否する（フェイルクローズ）。
        """
        self._allowed_dirs: list[Path] = []
        if allowed_dirs:
            for d in allowed_dirs:
                resolved = Path(d).resolve()
                self._allowed_dirs.append(resolved)
            logger.info(
                "LocalFileIngester initialized with allowed dirs: %s",
                [str(d) for d in self._allowed_dirs],
            )
        else:
            logger.info(
                "LocalFileIngester initialized without allowed dirs (fail-close)"
            )

    def _check_allowed_dirs_configured(self) -> None:
        """許可ディレクトリが設定されていることを確認する.

        Raises:
            ValueError: 許可ディレクトリが未設定の場合
        """
        if not self._allowed_dirs:
            raise ValueError(
                "ローカルファイルの取り込みが許可されていません。"
                "許可ディレクトリ（RAG_LOCAL_FILE_ALLOWED_DIRS）を設定してください。"
            )

    def _is_under_allowed_dir(self, resolved_path: Path) -> bool:
        """解決済みパスが許可ディレクトリ配下かどうかを検証する.

        Args:
            resolved_path: resolve() 済みの絶対パス

        Returns:
            許可ディレクトリ配下であれば True
        """
        for allowed_dir in self._allowed_dirs:
            try:
                resolved_path.relative_to(allowed_dir)
                return True
            except ValueError:
                continue
        return False

    def validate_identifier(self, identifier: str) -> str:
        """ファイルパスを検証し、正規化済みの絶対パスを返す.

        以下を検証する:
        - 許可ディレクトリが設定されていること
        - パスを正規化（シンボリックリンク解決・.. 展開）した上で、
          許可ディレクトリ配下であること
        - ファイルが存在すること
        - 通常ファイルであること（ディレクトリ・デバイスファイル等は拒否）
        - 拡張子が対応フォーマットに含まれること

        Args:
            identifier: 検証するファイルパス

        Returns:
            正規化済みの絶対パス文字列

        Raises:
            ValueError: バリデーション失敗時
        """
        self._check_allowed_dirs_configured()

        path = Path(identifier)
        resolved = path.resolve()

        # 許可ディレクトリチェック
        if not self._is_under_allowed_dir(resolved):
            raise ValueError(
                f"許可されていないディレクトリのファイルです: {identifier}"
            )

        # ファイル存在チェック
        if not resolved.exists():
            raise ValueError(f"ファイルが存在しません: {identifier}")

        # 通常ファイルチェック
        if not resolved.is_file():
            raise ValueError(f"通常ファイルではありません: {identifier}")

        # 拡張子チェック
        suffix = resolved.suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"未対応の拡張子です: {suffix} "
                f"(対応拡張子: {', '.join(sorted(ALLOWED_EXTENSIONS))})"
            )

        return str(resolved)

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """単一ファイルを読み込み IngestedContent を返す.

        Args:
            identifier: ファイルパス

        Returns:
            IngestedContent、または読み込み失敗時は None
        """
        try:
            validated_path = self.validate_identifier(identifier)
        except ValueError as e:
            logger.warning("Validation failed for %s: %s", identifier, e)
            raise

        path = Path(validated_path)

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("UTF-8 decode error: %s", validated_path)
            return None
        except PermissionError:
            logger.warning("Permission denied: %s", validated_path)
            return None
        except OSError as e:
            logger.warning("Failed to read file %s: %s", validated_path, e)
            return None

        # タイトルはファイル名（拡張子除去）から導出
        title = path.stem

        return IngestedContent(
            source_id=validated_path,
            title=title,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="local_file",
        )

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """ディレクトリ内のファイルを glob パターンで発見する.

        Args:
            source: ディレクトリパス
            **kwargs: pattern (str) — glob パターン（省略時はデフォルトパターン）

        Returns:
            発見されたファイルの絶対パスリスト
        """
        self._check_allowed_dirs_configured()

        dir_path = Path(source).resolve()

        # ディレクトリが許可ディレクトリ配下であることを検証
        if not self._is_under_allowed_dir(dir_path):
            raise ValueError(
                f"許可されていないディレクトリです: {source}"
            )

        if not dir_path.is_dir():
            raise ValueError(f"ディレクトリが存在しません: {source}")

        pattern = str(kwargs.get("pattern", ""))

        discovered: list[str] = []

        if pattern:
            # ユーザー指定の glob パターンを使用
            full_pattern = str(dir_path / pattern)
            for match in sorted(glob_module.glob(full_pattern, recursive=True)):
                match_path = Path(match).resolve()
                if (
                    match_path.is_file()
                    and match_path.suffix.lower() in ALLOWED_EXTENSIONS
                    and self._is_under_allowed_dir(match_path)
                ):
                    discovered.append(str(match_path))
        else:
            # デフォルト: ディレクトリ直下の対応拡張子ファイル
            for ext in sorted(ALLOWED_EXTENSIONS):
                for f in sorted(dir_path.glob(f"*{ext}")):
                    resolved_f = f.resolve()
                    if (
                        resolved_f.is_file()
                        and self._is_under_allowed_dir(resolved_f)
                    ):
                        discovered.append(str(resolved_f))

        logger.info(
            "Discovered %d files in %s (pattern=%r)",
            len(discovered),
            source,
            pattern or "(default)",
        )
        return discovered
