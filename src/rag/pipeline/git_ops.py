"""パイプライン制御 — git 操作.

仕様: docs/specs/pipeline-controller.md

source_store ディレクトリの git リポジトリを操作する。
git 操作はパイプライン制御のみが実行する。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# metadata.db 関連ファイルの .gitignore 内容
_GITIGNORE_CONTENT = """\
metadata.db
metadata.db-wal
metadata.db-shm
"""


class GitOperations:
    """source_store の git リポジトリ操作."""

    def __init__(self, repo_dir: Path) -> None:
        self._repo_dir = repo_dir

    def init_repo(self) -> None:
        """git リポジトリを初期化する.

        既に初期化済みの場合も .gitignore と user 設定を補正する。
        .gitignore で metadata.db を除外する。
        """
        git_dir = self._repo_dir / ".git"
        if not git_dir.exists():
            self._run(["git", "init"])
        # .gitignore 設定（既存リポジトリでも補正）
        self._ensure_gitignore()
        # パイプライン用のローカル git user 設定
        self._ensure_git_user()

    def commit(self, message: str, path: str | None = None) -> str | None:
        """ステージング + コミット.

        変更がない場合はスキップする。

        Args:
            message: コミットメッセージ
            path: 対象ディレクトリ（None で全体）

        Returns:
            コミット ID。変更なしの場合は None。
        """
        if path is not None:
            self._run(["git", "add", f"{path}/"])
            result = self._run(["git", "status", "--porcelain", "--", f"{path}/"])
        else:
            self._run(["git", "add", "-A"])
            result = self._run(["git", "status", "--porcelain"])
        if not result.stdout.strip():
            return None
        self._run(["git", "commit", "-m", message])
        return self.get_head_commit()

    def get_head_commit(self) -> str:
        """HEAD のコミットハッシュを取得する.

        Raises:
            subprocess.CalledProcessError: コミットが1つもない場合
        """
        result = self._run(["git", "rev-parse", "HEAD"])
        return result.stdout.strip()

    def has_uncommitted_changes(self, path: str | None = None) -> bool:
        """未コミットの変更があるか確認する（副作用なし）.

        Args:
            path: 対象ディレクトリ（None で全体）
        """
        if path is not None:
            result = self._run(
                ["git", "status", "--porcelain", "--", f"{path}/"],
            )
        else:
            result = self._run(["git", "status", "--porcelain"])
        return bool(result.stdout.strip())

    def has_commits(self) -> bool:
        """リポジトリにコミットが存在するか確認する."""
        try:
            self._run(["git", "rev-parse", "HEAD"])
        except subprocess.CalledProcessError:
            return False
        return True

    def is_commit_valid(self, commit_id: str) -> bool:
        """コミット ID が git 履歴に存在するか確認する."""
        try:
            result = self._run(["git", "cat-file", "-t", commit_id])
            return result.stdout.strip() == "commit"
        except subprocess.CalledProcessError:
            return False

    def get_diff(self, from_commit_id: str) -> list[tuple[str, str, str]]:
        """差分を取得する.

        呼び出し元が is_commit_valid で from_commit_id の妥当性を
        事前検証していることを前提とする。

        Args:
            from_commit_id: 基準コミット ID（40桁の16進ハッシュ）

        Returns:
            (status_char, file_path, old_path) のリスト。
            status_char: 'A', 'M', 'D', 'R'
            old_path: リネーム時のみ設定（それ以外は空文字列）
        """
        result = self._run([
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-status",
            f"{from_commit_id}..HEAD",
        ])
        return self._parse_diff_output(result.stdout)

    def list_all_files(self) -> list[str]:
        """HEAD で追跡されている全ファイルを列挙する.

        Returns:
            ファイルパスのリスト
        """
        result = self._run([
            "git",
            "-c",
            "core.quotepath=false",
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
        ])
        return [line for line in result.stdout.strip().splitlines() if line]

    @staticmethod
    def _parse_diff_output(output: str) -> list[tuple[str, str, str]]:
        """git diff --name-status の出力をパースする.

        Returns:
            (status_char, file_path, old_path) のリスト
        """
        entries: list[tuple[str, str, str]] = []
        for line in output.strip().splitlines():
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            if status.startswith("R"):
                # R100\told_path\tnew_path
                entries.append(("R", parts[2], parts[1]))
            else:
                entries.append((status[0], parts[1], ""))
        return entries

    def _ensure_gitignore(self) -> None:
        """metadata.db を .gitignore に含めることを保証する."""
        gitignore = self._repo_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
            return
        content = gitignore.read_text(encoding="utf-8")
        if "metadata.db" not in content:
            if not content.endswith("\n"):
                content += "\n"
            content += _GITIGNORE_CONTENT
            gitignore.write_text(content, encoding="utf-8")

    def _ensure_git_user(self) -> None:
        """パイプライン用のローカル git user 設定を保証する."""
        try:
            self._run(["git", "config", "user.name"])
        except subprocess.CalledProcessError:
            self._run(["git", "config", "user.name", "rag-pipeline"])
        try:
            self._run(["git", "config", "user.email"])
        except subprocess.CalledProcessError:
            self._run(["git", "config", "user.email", "rag-pipeline@localhost"])

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """git コマンドを実行する."""
        return subprocess.run(
            cmd,
            cwd=str(self._repo_dir),
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
        )
