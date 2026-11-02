-- Source only: forward publication (CONTRACT.md 3). bravo_fence and attachment_store are not published.
\set ON_ERROR_STOP on

CREATE PUBLICATION bravo_fwd FOR TABLE messages, bravo_barrier, attachments, message_attachments;

-- Active object store for attachment rows written on this side (D-04). The value comes from
-- 00-bravo-init.sh (BRAVO_ATTACHMENT_STORE_ID, default: the side name).
INSERT INTO attachment_store (id, active_store) VALUES (1, :'attachment_store_id');
