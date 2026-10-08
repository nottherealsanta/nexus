//! Decodes desktop wire deltas as described in `plans/in-progress/DESKTOP_OVERHAUL_PLAN.md` §3.3.

use crate::bridge::{Content, Snapshot};
use base64::Engine as _;
use serde_json::{Map, Value};
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

const IMAGE_LIMIT: usize = 9;
const IMAGE_BYTES_LIMIT: usize = 4 * 1024 * 1024;

/// Expands desktop wire deltas into the schema-2 suffix snapshots consumed by
/// the existing desktop presentation code.
#[derive(Default, Clone)]
pub struct Decoder {
    topics: HashMap<String, Value>,
    blocks: Vec<Content>,
    generation: Option<u64>,
    revision: Option<u64>,
    images: HashMap<String, (String, String)>,
}

impl Decoder {
    pub fn decode(&mut self, bytes: &[u8]) -> Result<Snapshot, String> {
        let value: Value = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
        let schema = value.get("schema").and_then(Value::as_u64).unwrap_or(0);
        if schema == 1 || schema == 2 {
            return serde_json::from_value(value).map_err(|e| e.to_string());
        }
        if schema != 3 {
            return Err(format!("unsupported snapshot schema {schema}"));
        }

        let reset = value.get("reset").and_then(Value::as_bool).unwrap_or(false);
        let generation = value
            .get("generation")
            .and_then(Value::as_u64)
            .ok_or_else(|| "schema 3 snapshot is missing generation".to_owned())?;
        let revision = value
            .get("revision")
            .and_then(Value::as_u64)
            .ok_or_else(|| "schema 3 snapshot is missing revision".to_owned())?;
        if self.revision.is_some_and(|previous| revision <= previous) {
            return Err("stale schema 3 revision".into());
        }
        if !reset && self.generation != Some(generation) {
            return Err("schema 3 generation changed without reset".into());
        }

        let mut next_images = if reset {
            HashMap::new()
        } else {
            self.images.clone()
        };
        if let Some(images) = value.get("images") {
            let images = images
                .as_object()
                .ok_or_else(|| "schema 3 images must be an object".to_owned())?;
            for id in images
                .get("drop")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let id = id
                    .as_str()
                    .ok_or_else(|| "schema 3 image drop id must be a string".to_owned())?;
                next_images.remove(id);
            }
            for image in images
                .get("put")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let id = image
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "schema 3 image put is missing id".to_owned())?;
                let media = image
                    .get("media")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "schema 3 image put is missing media".to_owned())?;
                let data = image
                    .get("data")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "schema 3 image put is missing data".to_owned())?;
                let decoded = base64::engine::general_purpose::STANDARD
                    .decode(data)
                    .map_err(|e| format!("invalid schema 3 image data: {e}"))?;
                if decoded.is_empty() || decoded.len() > IMAGE_BYTES_LIMIT {
                    return Err("schema 3 image exceeds cache limits".into());
                }
                if !matches!(
                    media,
                    "image/png" | "image/jpeg" | "image/gif" | "image/webp"
                ) {
                    return Err("unsupported schema 3 image media type".into());
                }
                next_images.insert(id.to_owned(), (media.to_owned(), data.to_owned()));
            }
        }
        let total_bytes: usize = next_images
            .values()
            .map(|(_, data)| {
                base64::engine::general_purpose::STANDARD
                    .decode(data)
                    .map_or(usize::MAX, |decoded| decoded.len())
            })
            .sum();
        if next_images.len() > IMAGE_LIMIT || total_bytes > IMAGE_BYTES_LIMIT * IMAGE_LIMIT {
            return Err("schema 3 image cache exceeds limits".into());
        }

        let topics = value
            .get("topics")
            .and_then(Value::as_object)
            .ok_or_else(|| "schema 3 topics must be an object".to_owned())?;
        let mut next_topics = if reset {
            HashMap::new()
        } else {
            self.topics.clone()
        };
        for (topic, fields) in topics {
            if !fields.is_object() {
                return Err(format!("schema 3 topic {topic:?} must be an object"));
            }
            next_topics.insert(topic.clone(), fields.clone());
        }

        let mut next_blocks: Cow<'_, [Content]> = if reset {
            Cow::Owned(Vec::new())
        } else {
            Cow::Borrowed(&self.blocks)
        };
        let mut blocks_from = next_blocks.len();
        if let Some(update) = value.get("blocks") {
            let operations = update
                .as_object()
                .ok_or_else(|| "schema 3 blocks update must be an object".to_owned())?;
            if operations.len() != 1 {
                return Err("schema 3 blocks update must contain exactly one operation".into());
            }
            if reset {
                blocks_from = 0;
                if update.get("splice").is_none() {
                    return Err("schema 3 reset must use a transcript splice".into());
                }
            }
            if let Some(splice) = update.get("splice") {
                let from = splice
                    .get("from")
                    .and_then(Value::as_u64)
                    .and_then(|v| usize::try_from(v).ok())
                    .ok_or_else(|| "schema 3 splice is missing a valid from".to_owned())?;
                if reset && from != 0 {
                    return Err("schema 3 reset splice must start at zero".into());
                }
                if from > next_blocks.len() {
                    return Err("schema 3 splice offset exceeds transcript length".into());
                }
                let replacement: Vec<Content> = serde_json::from_value(
                    splice
                        .get("blocks")
                        .cloned()
                        .ok_or_else(|| "schema 3 splice is missing blocks".to_owned())?,
                )
                .map_err(|e| format!("invalid schema 3 splice: {e}"))?;
                ensure_unique_ids(&replacement)?;
                next_blocks.to_mut().truncate(from);
                next_blocks.to_mut().extend(replacement);
                blocks_from = from;
            } else if let Some(appends) = update.get("append") {
                let appends: Vec<Value> = serde_json::from_value(appends.clone())
                    .map_err(|e| format!("invalid schema 3 append: {e}"))?;
                let mut seen = HashSet::new();
                for append in appends {
                    let id = append
                        .get("id")
                        .and_then(Value::as_str)
                        .ok_or_else(|| "schema 3 append is missing an id".to_owned())?;
                    if !seen.insert(id.to_owned()) {
                        return Err(format!("duplicate schema 3 append id {id:?}"));
                    }
                    let index = next_blocks
                        .iter()
                        .position(|block| block.id == id)
                        .ok_or_else(|| format!("unknown schema 3 append id {id:?}"))?;
                    let suffix = append
                        .get("text")
                        .and_then(Value::as_str)
                        .ok_or_else(|| "schema 3 append is missing text".to_owned())?;
                    let revision = append
                        .get("rev")
                        .cloned()
                        .ok_or_else(|| "schema 3 append is missing rev".to_owned())?;
                    let block = &mut next_blocks.to_mut()[index];
                    block.text.push_str(suffix);
                    block.rev = serde_json::from_value(revision)
                        .map_err(|e| format!("invalid schema 3 block revision: {e}"))?;
                    blocks_from = blocks_from.min(index);
                }
            } else {
                return Err("schema 3 blocks must contain splice or append".into());
            }
        } else if reset {
            return Err("schema 3 reset is missing its transcript splice".into());
        }
        ensure_unique_ids(&next_blocks)?;

        let mut metadata = Map::new();
        for fields in next_topics.values() {
            for (key, field) in fields.as_object().expect("validated topic object") {
                metadata.insert(key.clone(), field.clone());
            }
        }
        metadata.insert("schema".into(), Value::from(2));
        metadata.insert("generation".into(), Value::from(generation));
        metadata.insert("revision".into(), Value::from(revision));
        metadata.insert("blocks_from".into(), Value::from(blocks_from));
        let mut snapshot: Snapshot = serde_json::from_value(Value::Object(metadata))
            .map_err(|e| format!("invalid schema 3 metadata: {e}"))?;
        snapshot.blocks = next_blocks[blocks_from..].to_vec();
        if !snapshot.preview_image.is_empty() {
            if let Some((media, data)) = next_images.get(&snapshot.preview_image) {
                snapshot.preview_image = format!("data:{media};base64,{data}");
            } else {
                return Err(format!(
                    "unknown schema 3 preview image reference {:?}",
                    snapshot.preview_image
                ));
            }
        }
        for image in &mut snapshot.inline_images {
            if !image.data_ref.is_empty() {
                if let Some((_, data)) = next_images.get(&image.data_ref) {
                    image.data = data.clone();
                } else {
                    return Err(format!(
                        "unknown schema 3 inline image reference {:?}",
                        image.data_ref
                    ));
                }
            } else if !image.data.is_empty() {
                let decoded = base64::engine::general_purpose::STANDARD
                    .decode(&image.data)
                    .map_err(|e| format!("invalid inline image data: {e}"))?;
                if decoded.is_empty() || decoded.len() > IMAGE_BYTES_LIMIT {
                    return Err("inline image exceeds cache limits".into());
                }
                if !matches!(
                    image.media.as_str(),
                    "image/png" | "image/jpeg" | "image/gif" | "image/webp"
                ) {
                    return Err("unsupported inline image media type".into());
                }
            }
        }
        let inline_image_bytes: usize = snapshot
            .inline_images
            .iter()
            .map(|image| {
                base64::engine::general_purpose::STANDARD
                    .decode(&image.data)
                    .map_or(usize::MAX, |decoded| decoded.len())
            })
            .sum();
        if inline_image_bytes > IMAGE_BYTES_LIMIT * 8
            || snapshot.preview_image.len() > IMAGE_BYTES_LIMIT + 64
        {
            return Err("resolved schema 3 images exceed limits".into());
        }
        if next_images.len() > IMAGE_LIMIT {
            return Err("schema 3 image cache exceeds limits".into());
        }

        // Do not advance the decoder until every part, including metadata, has
        // passed validation.
        self.topics = next_topics;
        if let Cow::Owned(blocks) = next_blocks {
            self.blocks = blocks;
        }
        self.generation = Some(generation);
        self.revision = Some(revision);
        self.images = next_images;
        Ok(snapshot)
    }
}

fn ensure_unique_ids(blocks: &[Content]) -> Result<(), String> {
    let mut ids = HashSet::new();
    for block in blocks {
        if !ids.insert(block.id.as_str()) {
            return Err(format!("duplicate schema 3 block id {:?}", block.id));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decode(decoder: &mut Decoder, packet: &str) -> Result<Snapshot, String> {
        decoder.decode(packet.as_bytes())
    }

    #[test]
    fn reset_and_metadata_only_keep_transcript() {
        let mut decoder = Decoder::default();
        let first = decode(
            &mut decoder,
            r#"{"schema":3,"reset":true,"generation":4,"revision":1,"topics":{"header":{"title":"one"}},"blocks":{"splice":{"from":0,"blocks":[{"id":"a","text":"hello"}]}}}"#,
        )
        .unwrap();
        assert_eq!(first.blocks_from, 0);
        let next = decode(
            &mut decoder,
            r#"{"schema":3,"generation":4,"revision":2,"topics":{"header":{"title":"two"}}}"#,
        )
        .unwrap();
        assert_eq!(next.title, "two");
        assert_eq!(next.blocks_from, 1);
        assert!(next.blocks.is_empty());
    }

    #[test]
    fn unicode_append_splice_and_truncate() {
        let mut decoder = Decoder::default();
        decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":1,"revision":1,"topics":{},"blocks":{"splice":{"from":0,"blocks":[{"id":"a","text":"h"},{"id":"b"}]}}}"#).unwrap();
        let appended = decode(&mut decoder, r#"{"schema":3,"generation":1,"revision":2,"topics":{},"blocks":{"append":[{"id":"a","text":"é🙂","rev":"2"}]}}"#).unwrap();
        assert_eq!(appended.blocks_from, 0);
        assert_eq!(appended.blocks[0].text, "hé🙂");
        let truncated = decode(&mut decoder, r#"{"schema":3,"generation":1,"revision":3,"topics":{},"blocks":{"splice":{"from":1,"blocks":[]}}}"#).unwrap();
        assert_eq!(truncated.blocks_from, 1);
        assert!(truncated.blocks.is_empty());
    }

    #[test]
    fn reset_generation_stale_and_invalid_updates_are_transactional() {
        let mut decoder = Decoder::default();
        decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":1,"revision":5,"topics":{"header":{"title":"old"}},"blocks":{"splice":{"from":0,"blocks":[{"id":"a","text":"a"}]}}}"#).unwrap();
        assert!(decode(&mut decoder, r#"{"schema":3,"generation":2,"revision":6,"topics":{},"blocks":{"splice":{"from":0,"blocks":[]}}}"#).is_err());
        assert!(decode(
            &mut decoder,
            r#"{"schema":3,"generation":1,"revision":5,"topics":{},"blocks":{}}"#
        )
        .is_err());
        assert!(decode(
            &mut decoder,
            r#"{"schema":3,"reset":true,"generation":2,"revision":5,"topics":{},"blocks":{"splice":{"from":0,"blocks":[]}}}"#
        )
        .is_err());
        assert!(decode(
            &mut decoder,
            r#"{"schema":3,"reset":true,"generation":2,"revision":6,"topics":{},"blocks":{"append":[]}}"#
        )
        .is_err());
        assert!(decode(
            &mut decoder,
            r#"{"schema":3,"reset":true,"generation":2,"revision":6,"topics":{},"blocks":{"splice":{"from":1,"blocks":[]}}}"#
        )
        .is_err());
        assert!(decode(&mut decoder, r#"{"schema":3,"generation":1,"revision":6,"topics":{"header":{"title":"bad"}},"blocks":{"append":[{"id":"missing","text":"x","rev":"1"}]}}"#).is_err());
        let good = decode(&mut decoder, r#"{"schema":3,"generation":1,"revision":6,"topics":{},"blocks":{"append":[{"id":"a","text":"b","rev":"2"}]}}"#).unwrap();
        assert_eq!(good.title, "old");
        assert_eq!(good.blocks[0].text, "ab");
        let fresh = decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":2,"revision":7,"topics":{},"blocks":{"splice":{"from":0,"blocks":[]}}}"#).unwrap();
        assert!(fresh.blocks.is_empty());
        assert_eq!(fresh.blocks_from, 0);
    }

    #[test]
    fn schema_two_is_passthrough() {
        let mut decoder = Decoder::default();
        let snapshot = decoder.decode(br#"{"schema":2,"revision":9,"title":"legacy","blocks_from":0,"blocks":[{"id":"x"}]}"#).unwrap();
        assert_eq!(snapshot.title, "legacy");
        assert_eq!(snapshot.blocks[0].id, "x");
    }

    #[test]
    fn image_put_drop_and_reset_resolve_content_references() {
        let mut decoder = Decoder::default();
        let first = decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":1,"revision":1,"topics":{"header":{"preview_image":"sha256:abc"}},"images":{"put":[{"id":"sha256:abc","media":"image/png","data":"aGVsbG8="}]},"blocks":{"splice":{"from":0,"blocks":[]}}}"#).unwrap();
        assert_eq!(first.preview_image, "data:image/png;base64,aGVsbG8=");
        let missing = decode(&mut decoder, r#"{"schema":3,"generation":1,"revision":2,"topics":{"header":{"preview_image":""}},"images":{"drop":["sha256:abc"]}}"#).unwrap();
        assert!(missing.preview_image.is_empty());
        let reset = decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":2,"revision":3,"topics":{"header":{"preview_image":"sha256:abc"}},"images":{"put":[{"id":"sha256:abc","media":"image/jpeg","data":"aGVsbG8="}]},"blocks":{"splice":{"from":0,"blocks":[]}}}"#).unwrap();
        assert_eq!(reset.preview_image, "data:image/jpeg;base64,aGVsbG8=");
    }

    #[test]
    fn inline_image_references_resolve_and_dropped_references_fail_without_mutating_cache() {
        let mut decoder = Decoder::default();
        let first = decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":1,"revision":1,"topics":{"header":{"inline_images":[{"id":"draft","media":"image/png","data_ref":"sha256:abc"}]}},"images":{"put":[{"id":"sha256:abc","media":"image/png","data":"aGVsbG8="}]},"blocks":{"splice":{"from":0,"blocks":[]}}}"#).unwrap();
        assert_eq!(first.inline_images.len(), 1);
        assert_eq!(first.inline_images[0].data_ref, "sha256:abc");
        assert_eq!(first.inline_images[0].data, "aGVsbG8=");

        let dropped = r#"{"schema":3,"generation":1,"revision":2,"topics":{},"images":{"drop":["sha256:abc"]}}"#;
        assert!(decode(&mut decoder, dropped).is_err());

        let still_cached = decode(&mut decoder, r#"{"schema":3,"generation":1,"revision":3,"topics":{"header":{"inline_images":[{"id":"draft","media":"image/png","data_ref":"sha256:abc"}]}}}"#).unwrap();
        assert_eq!(still_cached.inline_images[0].data, "aGVsbG8=");
    }

    #[test]
    fn unknown_image_reference_is_rejected_transactionally() {
        let mut decoder = Decoder::default();
        let good = r#"{"schema":3,"reset":true,"generation":1,"revision":1,"topics":{"header":{"title":"safe"}},"blocks":{"splice":{"from":0,"blocks":[]}}}"#;
        decode(&mut decoder, good).unwrap();
        let malformed = r#"{"schema":3,"generation":1,"revision":2,"topics":{"header":{"title":"corrupt","preview_image":"sha256:missing"}}}"#;
        assert!(decode(&mut decoder, malformed).is_err());
        let unchanged = decode(
            &mut decoder,
            r#"{"schema":3,"generation":1,"revision":3,"topics":{}}"#,
        )
        .unwrap();
        assert_eq!(unchanged.title, "safe");
    }

    #[test]
    fn image_cache_rejects_oversized_or_invalid_data() {
        let mut decoder = Decoder::default();
        let too_large = "A".repeat(6 * 1024 * 1024);
        let packet = format!(
            r#"{{"schema":3,"reset":true,"generation":1,"revision":1,"topics":{{}},"images":{{"put":[{{"id":"large","media":"image/png","data":"{too_large}"}}]}},"blocks":{{"splice":{{"from":0,"blocks":[]}}}}}}"#
        );
        assert!(decode(&mut decoder, &packet).is_err());
        assert!(decode(&mut decoder, r#"{"schema":3,"reset":true,"generation":1,"revision":1,"topics":{},"images":{"put":[{"id":"bad","media":"image/svg+xml","data":"aGVsbG8="}]},"blocks":{"splice":{"from":0,"blocks":[]}}}"#).is_err());
    }
}
