"""テスト用設定デフォルト値."""

from __future__ import annotations

# RAGSettings の全必須フィールドのテスト用デフォルト値。
# config.py のフィールドにデフォルト値がないため、テストで RAGSettings を
# 直接生成する際にこの辞書を使用する。
# Optional (None デフォルト) フィールドは含まない。
# 注意: 本番 config.toml の値とは意図的に異なる場合がある（テスト用に安全・軽量な値を使用）。
TEST_SETTINGS_DEFAULTS: dict[str, object] = {
    # .env フィールド
    "embedding_provider": "local",
    "lmstudio_base_url": "http://localhost:1234",
    "chromadb_persist_dir": "./chroma_db",
    "bm25_persist_dir": "./bm25_index",
    "source_store_dir": "./source_store",
    "converted_store_dir": "./converted_store",
    "chromadb_server_host": "localhost",
    "chromadb_server_port": 8000,
    "chromadb_auto_start": False,
    "rag_transport": "stdio",
    "rag_http_host": "127.0.0.1",
    "rag_http_port": 8081,
    "rag_dns_rebinding_protection": True,
    "rag_debug_log_enabled": False,
    "rag_embedding_concurrency": 32,
    # config.toml フィールド
    "embedding_model_local": "nomic-embed-text",
    "embedding_model_online": "text-embedding-3-small",
    "embedding_prefix_enabled": True,
    "rag_embedding_retry_count": 3,
    "rag_embedding_retry_base_delay": 1.0,
    "rag_chunk_size": 200,
    "rag_chunk_overlap": 30,
    "rag_retrieval_count": 3,
    "rag_hybrid_search_enabled": False,
    "rag_vector_weight": 0.90,
    "rag_bm25_k1": 2.5,
    "rag_bm25_b": 0.50,
    "chromadb_collection_name": "knowledge",
    "rag_url_safety_check": False,
    "rag_url_safety_cache_ttl": 300,
    "rag_url_safety_timeout": 5.0,
    "rag_list_recent_limit": 20,
    "rag_log_file_max_bytes": 10_485_760,
    "rag_zenn_max_articles": 50,
    "rag_zenn_request_timeout": 30,
    "rag_zenn_request_interval": 1.0,
    "rag_html_remove_class_tokens": [
        "breadcrumb", "breadcrumbs", "topic-path", "nextprev", "pagination",
        "toolbar", "footer-wrapper", "footer", "scrollToFeedback", "suggest",
    ],
    "rag_document_supported_extensions": ".md,.txt,.pdf,.adoc,.jpg,.jpeg,.png,.webp,.mp4,.ts",
    "rag_document_http_mode_enabled": False,
    "rag_document_allowed_dirs": "",
    "rag_upload_max_file_size_mb": 50,
    "rag_upload_retry_after_write_sec": 30,
    "rag_upload_retry_after_rebuild_sec": 300,
    "rag_bluesky_appview_url": "https://public.api.bsky.app",
    "rag_bluesky_max_posts": 200,
    "rag_bluesky_request_timeout": 30,
    "rag_bluesky_request_interval": 1.0,
    "rag_bluesky_include_reposts": True,
    "rag_bluesky_force_youtube_reingest": False,
    # YouTube インジェスター
    "rag_youtube_whisper_model": "base",
    "rag_youtube_whisper_device": "cpu",
    # YouTube Fake モード（テストでは fake デフォルト）
    "rag_youtube_fake_mode": True,
    "rag_youtube_fake_fixture_dir": "src/rag/pipeline/ingesters/_fake/youtube/data",
    # BlueSky Fake モード（テストでは fake デフォルト）
    "rag_bluesky_fake_mode": True,
    "rag_bluesky_fake_fixture_dir": "src/rag/pipeline/ingesters/_fake/bluesky/data",
    # Zenn Fake モード（テストでは fake デフォルト）
    "rag_zenn_fake_mode": True,
    "rag_zenn_fake_fixture_dir": "src/rag/pipeline/ingesters/_fake/zenn/data",
    # Embedding Fake モード（テストでは fake デフォルト）
    "rag_embedding_fake_mode": True,
    "rag_embedding_fake_dimensions": 768,
    # Web (scrapy) Fake モード（テストでは fake デフォルト）
    "rag_web_fake_mode": True,
    "rag_scrapy_fake_mode": True,
    "rag_scrapy_fake_fixture_dir": "src/rag/scrapy/_fake/data",
    "rag_youtube_max_videos": 100,
    "rag_youtube_request_interval": 30.0,
    "rag_youtube_request_timeout": 30,
    "rag_youtube_transcript_languages": ["ja", "en"],
    "rag_youtube_merge_gap_sec": 2.0,
    "rag_youtube_merge_max_chars": 300,
    "rag_youtube_max_duration": 14400,
    # PDF バックエンド
    "rag_pdf_backend": "auto",
    "rag_pdf_mineru_mfd_conf_thres": 0.6,
    "rag_pdf_quality_ufffd_threshold": 0.10,
    "rag_pdf_quality_greek_threshold": 0.15,
    "rag_pdf_quality_cjk_min_threshold": 0.05,
    "rag_pdf_quality_min_chars_per_page": 10,
    "rag_pdf_quality_sample_pages": 10,
    # 青空文庫インジェスター
    "rag_aozora_max_works": 200,
    "rag_aozora_request_interval": 1.0,
    "rag_aozora_request_timeout": 30,
    # メディア解析（Vision モデル）
    "rag_vision_model": "google/gemma-4-26b-a4b",
    "rag_vision_reasoning_effort": "none",
    "rag_vision_frame_interval": 5,
    "rag_vision_max_tokens": 1024,
    "rag_vision_api_timeout": 180.0,
    # HNSW パラメータ
    "hnsw_m": 48,
    "hnsw_construction_ef": 400,
    "hnsw_search_ef": 300,
    # サイト一括取り込み（Scrapy subprocess）
    "site_ingest_temp_dir": ".tmp/site_ingest",
    "site_ingest_delay_sec": 0.1,
    "site_ingest_max_pages": 500,
    "site_ingest_download_timeout": 30,
    "site_ingest_timeout_sec": 7200,
    "site_ingest_error_count": 10,
}
