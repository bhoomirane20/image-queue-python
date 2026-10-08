CREATE TABLE IF NOT EXISTS jobs (
    id           BIGSERIAL PRIMARY KEY,
    state        TEXT NOT NULL DEFAULT 'queued'
                 CHECK (state IN ('queued','processing','done','failed','dead')),
    attempts     INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    filename     TEXT NOT NULL DEFAULT 'upload.bin',
    content_type TEXT NOT NULL DEFAULT '',
    src_bytes    INT NOT NULL DEFAULT 0,
    src_data     BYTEA,
    src_width    INT,
    src_height   INT,
    box_width    INT NOT NULL DEFAULT 320,
    box_height   INT NOT NULL DEFAULT 320,
    out_width    INT,
    out_height   INT,
    out_data     BYTEA,
    last_error   TEXT,
    leased_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs (state, id DESC);
CREATE INDEX IF NOT EXISTS jobs_leased ON jobs (leased_at) WHERE state = 'processing';
