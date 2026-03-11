"""LocalFileIngester: ローカルファイル取り込み用インジェスター

仕様: docs/specs/rag-knowledge.md（LocalFileIngester セクション）
Issue: #98
"""

from __future__ import annotations

import glob as glob_module
import logging
from pathlib import Path

from .base import BaseIngester, IngestedContent

logger = logging.getLogger(__name__)

# 対応する拡張子
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".md", ".txt"})


class LocalFileIngester(BaseIngester):
    """ローカルファイル取り込み用インジェスター.

    仕様: docs/specs/rag-knowledge.md（LocalFileIngester セクション）
    """

    def __init__(self, *, allowed_dirs: list[str] | None = None) -> None:
        """LocalFileIngester を初期化する.

        Args:
            allowed_dirs: 許可ディレクトリのリスト。
                未設定(None)または空リストの場合、ファイル取り込みを拒否する（フェイルクローズ）。
        """
        self._allowed_dirs: list[Path] = []
        if allowed_dirs:
            for d in allowed_dirs:
                d_stripped = d.strip()
                if d_stripped:
                    self._allowed_dirs.append(Path(d_stripped).resolve())

    def _check_allowed_dirs_configured(self) -> None:
        """許可ディレクトリが設定されていることを確認する.

        Raises:
            ValueError: 許可ディレクトリが未設定の場合
        """
        if not self._allowed_dirs:
            raise ValueError(
                "許可ディレクトリが設定されていません。"
                "RAG_LOCAL_FILE_ALLOWED_DIRS 環境変数を設定してください"
            )

    def _is_under_allowed_dir(self, resolved_path: Path) -> bool:
        """パスが許可ディレクトリ配下にあるか確認する.

        Args:
            resolved_path: 解決済みの絶対パス

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

        Args:
            identifier: 検証するファイルパス

        Returns:
            正規化済みの絶対パス文字列

        Raises:
            ValueError: バリデーション失敗時
        """
        if not identifier or not identifier.strip():
            raise ValueError("ファイルパスが空です")

        self._check_allowed_dirs_configured()

        # パスを正規化（シンボリックリンク解決・.. 展開）
        path = Path(identifier.strip())
        try:
            resolved = path.resolve(strict=True)
        except OSError as e:
            raise ValueError(f"ファイルが存在しません: {identifier}") from e

        # 許可ディレクトリ配下であることを検証
        if not self._is_under_allowed_dir(resolved):
            raise ValueError(
                f"許可ディレクトリ外のファイルです: {identifier}"
            )

        # 通常ファイルであることを検証
        if not resolved.is_file():
            raise ValueError(
                f"通常ファイルではありません: {identifier}"
            )

        # 拡張子チェック
        ext = resolved.suffix.lower()
        if ext not in _ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
            raise ValueError(
                f"未対応の拡張子です: {ext} (対応拡張子: {allowed})"
            )

        return str(resolved)

    async def fetch_single(self, identifier: str) -> IngestedContent | None:
        """ファイルパスを指定してローカルファイルを読み込む.

        Args:
            identifier: ファイルパス

        Returns:
            IngestedContent、または読み込み失敗時は None

        Raises:
            ValueError: バリデーション失敗時
        """
        resolved_path_str = self.validate_identifier(identifier)
        resolved = Path(resolved_path_str)

        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "UTF-8 デコードエラー: %s", resolved_path_str
            )
            return None
        except PermissionError:
            logger.warning(
                "読み取り権限がありません: %s", resolved_path_str
            )
            return None
        except OSError:
            logger.exception(
                "ファイル読み込みエラー: %s", resolved_path_str
            )
            return None

        # タイトルはファイル名（拡張子除去）から導出
        title = resolved.stem

        return IngestedContent(
            source_id=resolved_path_str,
            title=title,
            text=text,
            ingested_at=IngestedContent.now_iso(),
            source_type="local_file",
        )

    async def discover(self, source: str, **kwargs: object) -> list[str]:
        """ディレクトリパスと glob パターンでファイル一覧を返す.

        Args:
            source: ディレクトリパス
            **kwargs: pattern (str) - glob パターン（未指定時はデフォルト）

        Returns:
            バリデーション済みファイルパスのリスト
        """
        if not source or not source.strip():
            return []

        self._check_allowed_dirs_configured()

        source_path = Path(source.strip())
        resolved_source = source_path.resolve()

        if not resolved_source.is_dir():
            logger.warning("ディレクトリが存在しません: %s", source)
            return []

        # ディレクトリ自体が許可ディレクトリ配下にあるかチェック
        if not self._is_under_allowed_dir(resolved_source):
            raise ValueError(
                f"許可ディレクトリ外のディレクトリです: {source}"
            )

        pattern = kwargs.get("pattern")
        if pattern and isinstance(pattern, str):
            # パストラバーサル防止: glob パターンに ".." を含むことを禁止
            if ".." in pattern:
                raise ValueError(
                    "glob パターンに '..' は使用できません"
                )
            # ユーザー指定の glob パターン（解決済みパスを起点に使用）
            search_pattern = str(resolved_source / pattern)
            matched = glob_module.glob(search_pattern, recursive=True)
        else:
            # デフォルト: 直下の対応拡張子ファイルを全て対象
            matched = []
            for ext in sorted(_ALLOWED_EXTENSIONS):
                matched.extend(
                    glob_module.glob(str(resolved_source / f"*{ext}"))
                )

        # 各ファイルをバリデーション（許可ディレクトリチェック・拡張子チェック含む）
        valid_paths: list[str] = []
        for file_path in sorted(matched):
            try:
                validated = self.validate_identifier(file_path)
                valid_paths.append(validated)
            except ValueError as e:
                logger.debug("discover: スキップ %s (%s)", file_path, e)

        logger.info(
            "Discovered %d files in %s",
            len(valid_paths),
            source,
        )
        return valid_paths
