-- Synthetic prior ownership and immutable data; no version pragma or real paths.
INSERT INTO materials(material_id,source_kind,source_key,submitted_url,canonical_url,metadata_json,created_at,snapshot_key)
VALUES(41,'synthetic','original-key','synthetic://input','synthetic://canonical','{ "kept" : "中文" }','2099-01-01T00:00:00Z','original-version');
INSERT INTO distill_items(item_id,submitted_url,state,phase,material_id,queued_at,created_at,updated_at)
VALUES(51,'synthetic://input','queued','collecting',41,'2099-01-01T00:00:00Z','2099-01-01T00:00:00Z','2099-01-01T00:00:00Z');
-- Insert original media before freezing the SourceFact, respecting its guard.
INSERT INTO source_media VALUES(41,'image-1',0,'image/png','4259d3f5151edfc7205e3e7c3fde341e237968666aad8946dcbcf6a374502c60',X'00ff0d0a41');
INSERT INTO source_facts VALUES(61,41,'正文'||char(13)||char(10)||'é 原文','[ ]','{ "origin" : "synthetic" }','2099-01-01T00:00:00Z');
INSERT INTO submitted_sources(item_id,input_kind,input_key,input_label,input_metadata,content,retain_until)
VALUES(51,'direct_text','original-input','合成正文','{ "keep" : true }',X'00ff0d0a41',NULL);
INSERT INTO raw_counters VALUES('20261008',2);
INSERT INTO captures(capture_id,app_id,message_id,message_type,created_ms,received_ms,text,raw_id)
VALUES(81,'synthetic-app','synthetic-message','text',1,2,'附言'||char(13)||char(10)||'保留́','R-20261008-0002');
INSERT INTO capture_state VALUES(81,51,'synthetic-no-file.wav',NULL);
INSERT INTO capture_identity_events VALUES(91,81,'annotation','explicit synthetic user',0.75,'synthetic-target','2099-01-01T00:00:00Z');
INSERT INTO media_lifecycle VALUES(1,40,128,64);
INSERT INTO collection_operations(operation_id,kind,source_key,title,manifest_json,signature,content_signature,authority_json,confirmation_token,state,queued_at,created_at,updated_at)
VALUES(71,'same_topic','synthetic-collection','原合集','{ "members" : ["original"] }','original-signature','original-content-signature','{ "class" : "synthetic" }','synthetic-token','queued','2099-01-01T00:00:00Z','2099-01-01T00:00:00Z','2099-01-01T00:00:00Z');
INSERT INTO collection_members VALUES(71,1,'original-native','original-version',51,0,61,NULL);
INSERT INTO collection_confirmations VALUES('synthetic-token',0,1,71);
INSERT INTO collection_events VALUES(92,71,'synthetic','{ "keep" : true }','2099-01-01T00:00:00Z');
INSERT INTO collection_previews VALUES('synthetic-preview','{ "keep" : true }',NULL);
INSERT INTO delivery_adjacency VALUES('synthetic-app','synthetic-message','synthetic-target',3);
INSERT INTO feishu_binding VALUES('synthetic-app','synthetic-bot','synthetic-user','synthetic-chat',0,0);
INSERT INTO feishu_receipts(app_id,message_id,created_ms,raw_json,text,same_topic,content_kind,state)
VALUES('synthetic-app','synthetic-message',1,'{ "event" : "synthetic-only" }','原消息'||char(13)||char(10)||'保留',0,'links','accepted');
INSERT INTO feishu_parts VALUES('synthetic-app','synthetic-message',13,51,NULL,NULL);
INSERT INTO topic_entries(name,scope,updated_at) VALUES('原主题','synthetic','2099-01-01T00:00:00Z');
