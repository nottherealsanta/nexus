//! Client-dismissable notices and non-dismissable durable context (plan 13).
use super::*;

pub(super) fn draw(out: &mut Rows, b: &Content, width: usize, p: &Palette) {
    let dismissible = b.kind == "notice";
    let label = if dismissible { "Notice" } else { "Context" };
    let title = if b.title.is_empty() {
        label.to_string()
    } else {
        format!("{label}: {}", b.title)
    };
    let close_start = width.saturating_sub(3);
    let heading = crate::transcript::truncate(&format!("+ {title}"), close_start);
    let header = if dismissible && width >= 3 {
        format!(
            "{heading}{} {} ",
            " ".repeat(close_start.saturating_sub(heading.width())),
            if look::glyphs().ascii { "x" } else { "×" }
        )
    } else {
        heading
    };
    let operation = if dismissible && width >= 3 {
        Some(
            json!({"kind": "context_chips", "chips": [{"start": close_start, "end": width, "operation": b.operation}], "default": null}),
        )
    } else {
        None
    };
    out.push((
        Line::from(Span::styled(header, Style::default().fg(p.muted))),
        operation,
    ));
    for row in wrap(&[Span::raw(b.text.clone())], width.saturating_sub(2).max(1)) {
        let mut spans = vec![Span::styled("| ", Style::default().fg(p.muted))];
        spans.extend(row.into_iter().map(|cell| Span::styled(cell.g, cell.style)));
        out.push((Line::from(spans), None));
    }
    out.push((
        Line::from(Span::styled("+", Style::default().fg(p.muted))),
        None,
    ));
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn notice_close_is_an_isolated_three_cell_target() {
        let p = Palette::new(false);
        let operation = json!({"kind":"dismiss_notice", "id":"s:1"});
        for width in [8, 24, 80] {
            let b = Content {
                kind: "notice".into(),
                title: "A long title".into(),
                text: "body".into(),
                operation: Some(operation.clone()),
                ..Default::default()
            };
            let rows = build(&b, width, &p);
            let op = rows[0].1.as_ref().unwrap();
            assert_eq!(op["chips"][0]["start"], usize::from(width) - 3);
            assert_eq!(op["chips"][0]["end"], width);
            assert_eq!(op["chips"][0]["operation"], operation);
            assert!(op["default"].is_null());
            assert!(rows[1..].iter().all(|(_, op)| op.is_none()));
        }
    }

    #[test]
    fn context_has_no_dismiss_action() {
        let p = Palette::new(false);
        let b = Content {
            kind: "context_notice".into(),
            text: "durable".into(),
            operation: Some(json!({"kind":"dismiss_notice"})),
            ..Default::default()
        };
        let mut rows = Vec::new();
        draw(&mut rows, &b, 30, &p);
        assert!(rows.iter().all(|(_, op)| op.is_none()));
    }
}
