CREATE TABLE IF NOT EXISTS shot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode TEXT NOT NULL,
    shot_no INTEGER NOT NULL CHECK (shot_no > 0),
    desc TEXT NOT NULL CHECK (length(trim(desc)) > 0),
    dialogue TEXT NOT NULL,
    camera TEXT NOT NULL CHECK (length(trim(camera)) > 0),
    duration_s INTEGER NOT NULL CHECK (duration_s > 0),
    characters_json TEXT NOT NULL,
    UNIQUE (episode, shot_no)
);

CREATE INDEX IF NOT EXISTS idx_shot_episode_no ON shot (episode, shot_no);

CREATE TABLE IF NOT EXISTS persona (
    name TEXT PRIMARY KEY CHECK (length(trim(name)) > 0),
    appearance TEXT NOT NULL CHECK (length(trim(appearance)) > 0),
    outfit TEXT NOT NULL CHECK (length(trim(outfit)) > 0),
    style_tokens_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recipe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode TEXT NOT NULL CHECK (length(trim(episode)) > 0),
    shot_no INTEGER NOT NULL CHECK (shot_no > 0),
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    prompt TEXT NOT NULL CHECK (length(trim(prompt)) > 0),
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    scene_constraint TEXT CHECK (
        scene_constraint IS NULL OR length(trim(scene_constraint)) > 0
    ),
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    size TEXT NOT NULL CHECK (length(trim(size)) > 0),
    reference_asset_ids_json TEXT NOT NULL CHECK (
        length(trim(reference_asset_ids_json)) > 0
    ),
    seed INTEGER CHECK (seed IS NULL OR seed >= 0),
    source_request_id TEXT CHECK (
        source_request_id IS NULL OR length(trim(source_request_id)) > 0
    ),
    target_request_id TEXT CHECK (
        target_request_id IS NULL OR length(trim(target_request_id)) > 0
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((source_request_id IS NULL) = (target_request_id IS NULL)),
    CHECK (source_request_id IS NULL OR source_request_id != target_request_id),
    UNIQUE (episode, shot_no, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_recipe_episode_shot
ON recipe (episode, shot_no, created_at);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL UNIQUE CHECK (length(trim(reservation_id)) > 0),
    job TEXT NOT NULL CHECK (length(trim(job)) > 0),
    project TEXT NOT NULL CHECK (length(trim(project)) > 0),
    episode TEXT NOT NULL CHECK (length(trim(episode)) > 0),
    shot_no INTEGER NOT NULL CHECK (shot_no > 0),
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    est_fen INTEGER NOT NULL CHECK (est_fen > 0),
    actual_fen INTEGER CHECK (actual_fen IS NULL OR actual_fen >= 0),
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    provider_job_id TEXT CHECK (
        provider_job_id IS NULL OR length(trim(provider_job_id)) > 0
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'reserved', 'submitted', 'succeeded', 'failed',
            'unknown', 'settled', 'released'
        )
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status != 'settled' OR actual_fen IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_ledger_budget_scope
ON ledger (project, episode, shot_no, created_at);

-- 生图结果与财务状态分开保存：结算并不表示画面成功。
CREATE TABLE IF NOT EXISTS image_result (
    reservation_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 人工终审不可静默覆盖；被拒绝的画面进入带原因的返工队列。
CREATE TABLE IF NOT EXISTS qc_review (
    source_request_id TEXT PRIMARY KEY CHECK (length(trim(source_request_id)) > 0),
    label_json TEXT NOT NULL CHECK (length(trim(label_json)) > 0),
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rework_queue (
    source_request_id TEXT PRIMARY KEY CHECK (length(trim(source_request_id)) > 0),
    failure_reasons_json TEXT NOT NULL CHECK (length(trim(failure_reasons_json)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'cancelled')
    ),
    target_request_id TEXT CHECK (
        target_request_id IS NULL OR length(trim(target_request_id)) > 0
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 人工终审后的可检索素材索引；文件留在磁盘，SQLite 只保存定位和标签。
CREATE TABLE IF NOT EXISTS archive_asset (
    source_request_id TEXT PRIMARY KEY CHECK (length(trim(source_request_id)) > 0),
    project TEXT NOT NULL CHECK (length(trim(project)) > 0),
    episode TEXT NOT NULL CHECK (length(trim(episode)) > 0),
    shot_no INTEGER NOT NULL CHECK (shot_no > 0),
    characters_json TEXT NOT NULL CHECK (length(trim(characters_json)) > 0),
    image_path TEXT NOT NULL CHECK (length(trim(image_path)) > 0),
    metadata_path TEXT NOT NULL CHECK (length(trim(metadata_path)) > 0),
    image_sha256 TEXT NOT NULL CHECK (length(image_sha256) = 64),
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_archive_scope
ON archive_asset (project, episode, shot_no, approved, created_at);
