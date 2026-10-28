//! As-you-type composer completion: ranking and acceptance text, copied from
//! old Nexus (`ui_support/completion.py` and the old TUI's `rank_commands`,
//! `accepted_completion`, `completion_candidates`). Pure functions; the app
//! owns the state and the Core requests.

/// Rows the `/` and `@` menus always show (fewer only when fewer exist), so the
/// popup keeps its height while typing.
pub const MENU_LIMIT: usize = 10;

/// The token at the end of `prefix`: the text after the last whitespace.
pub fn token(prefix: &str) -> &str {
    prefix.rsplit(char::is_whitespace).next().unwrap_or("")
}

/// Edit distance with adjacent swaps, so `mew` is one step from `new`.
pub fn edit_distance(a: &str, b: &str) -> usize {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    let mut rows = vec![(0..=b.len()).collect::<Vec<_>>()];
    for i in 1..=a.len() {
        let mut row = vec![i];
        for j in 1..=b.len() {
            let mut c = (rows[i - 1][j] + 1)
                .min(row[j - 1] + 1)
                .min(rows[i - 1][j - 1] + usize::from(a[i - 1] != b[j - 1]));
            if i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1] {
                c = c.min(rows[i - 2][j - 2] + 1);
            }
            row.push(c);
        }
        rows.push(row);
    }
    rows[a.len()][b.len()]
}

fn is_subsequence(needle: &str, name: &str) -> bool {
    let mut rest = name.chars();
    needle.chars().all(|c| rest.any(|n| n == c))
}

/// Rank `(value, match names)` rows for `query` and pad to `limit`. Tiers: prefix,
/// substring or subsequence, typo (distance <= 1, or <= 2 from five characters),
/// then the remaining rows in their given order.
pub fn rank_menu(query: &str, rows: &[(String, Vec<String>)], limit: usize) -> Vec<String> {
    let needle = query.to_lowercase();
    let tolerance = if needle.chars().count() < 5 { 1 } else { 2 };
    let mut scored: Vec<(usize, usize, &String)> = rows
        .iter()
        .enumerate()
        .map(|(index, (value, names))| {
            let tier = names
                .iter()
                .map(|name| {
                    let name = name.to_lowercase();
                    if name.starts_with(&needle) {
                        0
                    } else if name.contains(&needle) || is_subsequence(&needle, &name) {
                        1
                    } else if !needle.is_empty()
                        && (edit_distance(
                            &needle,
                            &name
                                .chars()
                                .take(needle.chars().count() + 1)
                                .collect::<String>(),
                        ) <= tolerance
                            || edit_distance(&needle, &name) <= tolerance)
                    {
                        2
                    } else {
                        3
                    }
                })
                .min()
                .unwrap_or(3);
            (tier, index, value)
        })
        .collect();
    scored.sort();
    scored
        .into_iter()
        .take(limit)
        .map(|(_, _, value)| value.clone())
        .collect()
}

/// Exact hits first, then typo and fuzzy matches and padding from `pool`
/// (old `file_menu`): a short list never shrinks the menu below ten rows.
pub fn file_menu(query: &str, hits: &[String], pool: &[String]) -> Vec<String> {
    if hits.len() >= MENU_LIMIT {
        return hits.to_vec();
    }
    let rows: Vec<(String, Vec<String>)> = pool
        .iter()
        .filter(|path| !hits.contains(path))
        .map(|path| {
            let base = path.trim_end_matches('/').rsplit('/').next().unwrap_or("");
            (path.clone(), vec![base.to_owned()])
        })
        .collect();
    let mut menu = hits.to_vec();
    menu.extend(rank_menu(query, &rows, MENU_LIMIT));
    menu.truncate(MENU_LIMIT.max(hits.len()));
    menu
}

/// Text inserted for an accepted completion. An `@` directory (`dir/`) gets no
/// trailing space so completion continues inside it; everything else is
/// followed by a space.
pub fn accepted_completion(token: &str, value: &str) -> String {
    let at = token.starts_with('@');
    format!(
        "{}{value}{}",
        if at { "@" } else { "" },
        if at && value.ends_with('/') { "" } else { " " }
    )
}

/// Candidates for `prefix` from replies cached by prefix. Only ancestor requests
/// in the same token context are reused, so rows stay visible while typing and
/// backspacing. `None` when no cached reply applies yet.
pub fn candidates(cache: &[(String, Vec<String>)], prefix: &str) -> Option<Vec<String>> {
    let token = token(prefix);
    let context = &prefix[..prefix.len() - token.len()];
    let (key, values) = cache
        .iter()
        .rev()
        .filter(|(key, _)| {
            let old = self::token(key);
            &key[..key.len() - old.len()] == context && token.starts_with(old)
        })
        .max_by_key(|(key, _)| key.len())?;
    if key == prefix {
        return Some(values.clone());
    }
    let needle = token.trim_start_matches('@').to_lowercase();
    let (mut hits, rest): (Vec<_>, Vec<_>) = values
        .iter()
        // An accepted `dir/` must not offer itself again.
        .filter(|value| !(value.ends_with('/') && value.to_lowercase() == needle))
        .partition(|value| value.to_lowercase().starts_with(&needle));
    // File menus keep their height while Core re-ranks.
    hits.extend(rest);
    hits.truncate(MENU_LIMIT);
    Some(hits.into_iter().cloned().collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rows(names: &[(&str, &[&str])]) -> Vec<(String, Vec<String>)> {
        names
            .iter()
            .map(|(v, n)| {
                (
                    (*v).to_owned(),
                    std::iter::once(*v)
                        .chain(n.iter().copied())
                        .map(|s| s.trim_start_matches('/').to_owned())
                        .collect(),
                )
            })
            .collect()
    }

    #[test]
    fn edit_distance_counts_adjacent_swaps_once() {
        assert_eq!(edit_distance("mew", "new"), 1);
        assert_eq!(edit_distance("nwe", "new"), 1);
        assert_eq!(edit_distance("abc", "abc"), 0);
        assert_eq!(edit_distance("abc", "xyz"), 3);
    }

    #[test]
    fn rank_menu_orders_prefix_then_substring_then_typo_then_rest() {
        let r = rows(&[
            ("/compact", &[]),
            ("/model", &[]),
            ("/new", &[]),
            ("/remodel", &[]),
            ("/theme", &[]),
        ]);
        assert_eq!(
            rank_menu("mo", &r, 10),
            ["/model", "/remodel", "/compact", "/new", "/theme"]
        );
        // `mew` is a typo of `new` (tier 2), ahead of the unrelated rest.
        assert_eq!(rank_menu("mew", &r, 10)[0], "/new");
        // An alias counts as a name.
        let aliased = rows(&[("/theme", &[]), ("/settings", &["/prefs"])]);
        assert_eq!(rank_menu("pre", &aliased, 10)[0], "/settings");
        // The limit caps the menu; an empty needle keeps the given order.
        assert_eq!(rank_menu("", &r, 2), ["/compact", "/model"]);
    }

    #[test]
    fn subsequence_matches_rank_above_the_rest() {
        let r = rows(&[("/zzz", &[]), ("/compact", &[])]);
        assert_eq!(rank_menu("cpt", &r, 10)[0], "/compact");
    }

    #[test]
    fn accepted_text_keeps_directories_open() {
        assert_eq!(accepted_completion("@sr", "src/a.rs"), "@src/a.rs ");
        assert_eq!(accepted_completion("@sr", "src/"), "@src/");
        assert_eq!(accepted_completion("/mo", "/model"), "/model ");
    }

    #[test]
    fn file_menu_pads_short_hit_lists_from_the_pool() {
        let pool: Vec<String> = (0..20).map(|i| format!("dir/file{i}.rs")).collect();
        let hits = vec!["src/main.rs".to_owned()];
        let menu = file_menu("mian", &hits, &pool);
        assert_eq!(menu.len(), MENU_LIMIT);
        assert_eq!(menu[0], "src/main.rs");
        // Ten or more hits are left alone.
        let many: Vec<String> = (0..12).map(|i| format!("f{i}")).collect();
        assert_eq!(file_menu("f", &many, &pool), many);
        // A small pool gives fewer rows only because fewer exist.
        assert_eq!(file_menu("x", &[], &["a".into()]), ["a"]);
    }

    #[test]
    fn cached_replies_serve_extended_and_shortened_tokens() {
        let cache = vec![(
            "see @s".to_owned(),
            vec!["src/".to_owned(), "setup.py".to_owned(), "lib/".to_owned()],
        )];
        assert_eq!(candidates(&cache, "see @s").unwrap().len(), 3);
        // Typing on: matches first, the rest keeps the menu filled.
        assert_eq!(
            candidates(&cache, "see @se").unwrap(),
            ["setup.py", "src/", "lib/"]
        );
        // The accepted directory does not offer itself.
        assert!(!candidates(&cache, "see @src/")
            .unwrap()
            .contains(&"src/".to_owned()));
        // Another token context, or a shorter token, has no cache.
        assert!(candidates(&cache, "@s").is_none());
        assert!(candidates(&cache, "see @").is_none());
    }
}
