//! The built-in code editor, using the vendored `ratatui-code-editor` (MIT).
//!
//! The vendored crate supplies the rope, Tree-sitter highlighting, selections,
//! folds, marks, undo history and diff views. Nexus supplies the keys (the crate
//! has no crossterm input), the theme and the clipboard, and keeps the cursor
//! and scroll position across openings.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::Widget;
use ratatui::Frame;
use ratatui_code_editor::actions::{
    Copy, Cut, Delete, DeleteLine, Duplicate, Indent, InsertNewline, InsertText, MoveDown,
    MoveLeft, MoveRight, MoveUp, Paste, Redo, SelectAll, ToggleComment, UnIndent, Undo,
};
use ratatui_code_editor::editor::Editor;
use ratatui_code_editor::types::{CodeFoldingOptions, FoldIndicators};
use ratatui_code_editor::{selection::Selection, theme as editor_theme};
use std::collections::HashMap;

use crate::theme::Theme;

/// Map the shared theme tokens to the vendored editor's syntax names.
pub fn styles_from_theme(theme: &Theme) -> HashMap<String, Style> {
    let fg = |token: &str, modifiers: Modifier| {
        Style::default()
            .fg(theme.color(token).unwrap_or(Color::Reset))
            .add_modifier(modifiers)
    };
    let bg = |token: &str| Style::default().bg(theme.color(token).unwrap_or(Color::Reset));
    let mut styles = HashMap::new();
    for name in [
        "identifier",
        "field_identifier",
        "property_identifier",
        "property",
        "variable",
        "variable.builtin",
    ] {
        styles.insert(name.to_string(), fg("text.primary", Modifier::empty()));
    }
    for (name, token, modifiers) in [
        ("string", "status.success", Modifier::empty()),
        ("keyword", "interactive.accent", Modifier::BOLD),
        ("constant", "status.warning", Modifier::empty()),
        ("number", "status.warning", Modifier::empty()),
        ("integer", "status.warning", Modifier::empty()),
        ("float", "status.warning", Modifier::empty()),
        ("function", "agent.blue", Modifier::empty()),
        ("function.call", "agent.blue", Modifier::empty()),
        ("method", "agent.blue", Modifier::empty()),
        ("function.macro", "agent.blue", Modifier::empty()),
        ("namespace", "agent.violet", Modifier::empty()),
        ("type", "status.info", Modifier::empty()),
        ("type.builtin", "status.info", Modifier::empty()),
        ("tag", "interactive.multi", Modifier::empty()),
        ("tag.attribute", "interactive.multi", Modifier::empty()),
        ("comment", "text.muted", Modifier::ITALIC),
        ("error", "status.error", Modifier::BOLD),
        ("line_number", "text.muted", Modifier::empty()),
        ("fold_separator", "text.muted", Modifier::empty()),
    ] {
        styles.insert(name.to_string(), fg(token, modifiers));
    }
    styles.insert("default_text".into(), fg("text.primary", Modifier::empty()));
    styles.insert("diff_added".into(), bg("diff.add_bg"));
    styles.insert("diff_added_word".into(), bg("diff.add_bg"));
    styles.insert("diff_deleted".into(), bg("diff.del_bg"));
    styles.insert("diff_deleted_word".into(), bg("diff.del_bg"));
    styles.insert("word_highlight".into(), bg("interactive.selection"));
    styles.insert("selection".into(), bg("interactive.selection"));
    styles
}

/// The language name the vendored crate knows for a path.
pub fn language_for_path(path: &str) -> &'static str {
    let name = path.rsplit('/').next().unwrap_or(path).to_ascii_lowercase();
    let extension = name.rsplit_once('.').map_or("", |(_, ext)| ext);
    match extension {
        "rs" => "rust",
        "py" | "pyi" => "python",
        "js" | "jsx" | "mjs" | "cjs" => "javascript",
        "ts" | "tsx" | "mts" | "cts" => "typescript",
        "go" => "go",
        "c" | "h" => "c",
        "html" | "htm" => "html",
        "css" => "css",
        "yaml" | "yml" => "yaml",
        "json" => "json",
        "toml" => "toml",
        "sh" | "bash" | "zsh" => "shell",
        "md" | "markdown" => "markdown",
        _ => "text",
    }
}

/// A file open in the code editor.
pub struct CodeEditor {
    editor: Editor,
    path: String,
    language: &'static str,
}

impl CodeEditor {
    pub fn new(path: &str, text: &str, ascii: bool) -> Result<Self, String> {
        Self::with_theme(path, text, ascii, None)
    }

    /// Open with the shared theme's tokens; `None` keeps the crate's palette.
    pub fn with_theme(
        path: &str,
        text: &str,
        ascii: bool,
        theme: Option<&Theme>,
    ) -> Result<Self, String> {
        let language = language_for_path(path);
        let mut editor = match theme {
            Some(theme) => Editor::new_with_theme(language, text, styles_from_theme(theme)),
            None => Editor::new(language, text, editor_theme::vesper()),
        }
        .map_err(|error| error.to_string())?;
        if ascii {
            editor.set_code_folding_options(CodeFoldingOptions {
                enabled: true,
                indicators: FoldIndicators::ascii(),
            });
        }
        Ok(Self {
            editor,
            path: path.to_string(),
            language,
        })
    }

    pub fn set_theme(&mut self, theme: &Theme) {
        self.editor.set_theme(styles_from_theme(theme));
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    pub fn language(&self) -> &str {
        self.language
    }

    pub fn text(&self) -> String {
        self.editor.get_content()
    }

    pub fn set_text(&mut self, text: &str) {
        self.editor.set_content(text);
    }

    /// Draw the editor and place the terminal cursor.
    pub fn render(&mut self, frame: &mut Frame, area: Rect) {
        self.editor.focus(&area);
        frame.render_widget(&self.editor, area);
        if let Some((x, y)) = self.editor.get_visible_cursor(&area) {
            frame.set_cursor_position((x, y));
        }
    }

    /// Draw into a buffer at `area`; `cursor` places the terminal cursor later.
    pub fn render_buffer(&mut self, buf: &mut Buffer, area: Rect) {
        self.editor.focus(&area);
        Widget::render(&self.editor, area, buf);
    }

    pub fn cursor(&self, area: Rect) -> Option<(u16, u16)> {
        self.editor.get_visible_cursor(&area)
    }

    /// Apply a key. Returns whether the editor used it.
    pub fn key(&mut self, key: KeyEvent, area: Rect) -> bool {
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        match key.code {
            KeyCode::Char('z' | 'Z') if ctrl && shift => self.editor.apply(Redo),
            KeyCode::Char('z') if ctrl => self.editor.apply(Undo),
            KeyCode::Char('y') if ctrl => self.editor.apply(Redo),
            KeyCode::Char('c') if ctrl => self.editor.apply(Copy),
            KeyCode::Char('v') if ctrl => self.editor.apply(Paste),
            KeyCode::Char('x') if ctrl => self.editor.apply(Cut),
            KeyCode::Char('k') if ctrl => self.editor.apply(DeleteLine),
            KeyCode::Char('d') if ctrl => self.editor.apply(Duplicate),
            KeyCode::Char('a') if ctrl => self.editor.apply(SelectAll),
            KeyCode::Char('u') if ctrl => self.editor.apply(UnIndent),
            KeyCode::Char('/') if alt => self.editor.apply(ToggleComment),
            KeyCode::Left if ctrl => self.move_word(shift, false),
            KeyCode::Right if ctrl => self.move_word(shift, true),
            KeyCode::Left => self.editor.apply(MoveLeft { shift }),
            KeyCode::Right => self.editor.apply(MoveRight { shift }),
            KeyCode::Up => self.editor.apply(MoveUp { shift }),
            KeyCode::Down => self.editor.apply(MoveDown { shift }),
            KeyCode::Home => self.line_edge(shift, false),
            KeyCode::End => self.line_edge(shift, true),
            KeyCode::PageUp => self.page(shift, false, area.height),
            KeyCode::PageDown => self.page(shift, true, area.height),
            KeyCode::Backspace => self.editor.apply(Delete),
            KeyCode::Delete => self.delete_forward(),
            KeyCode::Enter => self.editor.apply(InsertNewline),
            KeyCode::Tab => self.editor.apply(Indent),
            KeyCode::BackTab => self.editor.apply(UnIndent),
            KeyCode::Char(c) if !ctrl => self.editor.apply(InsertText {
                text: c.to_string(),
            }),
            _ => return false,
        }
        self.editor.focus(&area);
        true
    }

    pub fn mouse(&mut self, mouse: MouseEvent, area: Rect) -> bool {
        match mouse.kind {
            MouseEventKind::ScrollUp => {
                self.editor.scroll_up();
                true
            }
            MouseEventKind::ScrollDown => {
                self.editor.scroll_down(area.height as usize);
                true
            }
            MouseEventKind::Down(MouseButton::Left) => {
                if self
                    .editor
                    .toggle_fold_at_mouse(mouse.column, mouse.row, &area)
                {
                    return true;
                }
                if self
                    .editor
                    .expand_hidden_diff_at_mouse(mouse.column, mouse.row, &area)
                {
                    return true;
                }
                if let Some(cursor) = self
                    .editor
                    .cursor_from_mouse(mouse.column, mouse.row, &area)
                {
                    self.editor.handle_mouse_down(cursor);
                }
                true
            }
            MouseEventKind::Drag(MouseButton::Left) => {
                if let Some(cursor) = self
                    .editor
                    .cursor_from_mouse(mouse.column, mouse.row, &area)
                {
                    self.editor.handle_mouse_drag(cursor);
                }
                true
            }
            MouseEventKind::Up(MouseButton::Left) => true,
            _ => false,
        }
    }

    fn set_cursor(&mut self, cursor: usize, shift: bool) {
        if shift {
            self.editor.extend_selection(cursor);
        } else {
            self.editor.clear_selection();
        }
        self.editor.set_cursor(cursor);
    }

    fn word(&self) -> (usize, usize) {
        let cursor = self.editor.get_cursor();
        self.editor.code_ref().word_boundaries(cursor)
    }

    fn move_word(&mut self, shift: bool, forward: bool) {
        let (start, end) = self.word();
        let cursor = self.editor.get_cursor();
        let target = if forward {
            if cursor < end {
                end
            } else {
                let code = self.editor.code_ref();
                let next = code.next_grapheme_boundary(end);
                code.word_boundaries(next).1
            }
        } else if cursor > start {
            start
        } else {
            let code = self.editor.code_ref();
            let previous = code.prev_grapheme_boundary(start);
            code.word_boundaries(previous).0
        };
        self.set_cursor(target, shift);
    }

    fn line_edge(&mut self, shift: bool, end: bool) {
        let code = self.editor.code_ref();
        let cursor = self.editor.get_cursor();
        let (line, _) = code.point(cursor);
        let start = code.line_to_char(line);
        let target = if end {
            start + code.line_len(line)
        } else {
            let text = code.slice(start, start + code.line_len(line));
            let indent = text.len() - text.trim_start_matches([' ', '\t']).len();
            if cursor == start + indent {
                start
            } else {
                start + indent
            }
        };
        self.set_cursor(target, shift);
    }

    fn page(&mut self, shift: bool, down: bool, height: u16) {
        let step = usize::from(height.saturating_sub(2).max(1));
        for _ in 0..step {
            if down {
                self.editor.apply(MoveDown { shift });
            } else {
                self.editor.apply(MoveUp { shift });
            }
        }
    }

    fn delete_forward(&mut self) {
        let cursor = self.editor.get_cursor();
        let code = self.editor.code_mut();
        if cursor < code.len() {
            let next = code.next_grapheme_boundary(cursor);
            code.tx();
            code.set_state_before(cursor, None);
            code.remove(cursor, next);
            code.set_state_after(cursor, None);
            code.commit();
            self.editor.reset_highlight_cache();
        }
    }

    /// Select text (tests and find/replace).
    pub fn select(&mut self, start: usize, end: usize) {
        self.editor
            .set_selection(Some(Selection::from_anchor_and_cursor(start, end)));
        self.editor.set_cursor(end);
    }
}

/// Mutable editor state rendered from the shell's shared render reference.
pub struct CodeBuffer(std::cell::RefCell<CodeEditor>);

impl std::fmt::Debug for CodeBuffer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CodeBuffer").finish_non_exhaustive()
    }
}

impl CodeBuffer {
    pub fn new(path: &str, text: &str, ascii: bool, theme: &Theme) -> Result<Self, String> {
        Ok(Self(std::cell::RefCell::new(CodeEditor::with_theme(
            path,
            text,
            ascii,
            Some(theme),
        )?)))
    }
    pub fn text(&self) -> String {
        self.0.borrow().text()
    }
    pub fn set_text(&self, text: &str) {
        self.0.borrow_mut().set_text(text);
    }
    pub fn insert_str(&self, text: &str) {
        self.0.borrow_mut().editor.apply(InsertText {
            text: text.to_owned(),
        });
    }
    pub fn key(&self, key: KeyEvent, area: Rect) -> bool {
        self.0.borrow_mut().key(key, area)
    }
    pub fn mouse(&self, mouse: MouseEvent, area: Rect) -> bool {
        self.0.borrow_mut().mouse(mouse, area)
    }
    pub fn render(&self, buf: &mut Buffer, area: Rect, theme: &Theme) {
        let mut editor = self.0.borrow_mut();
        editor.set_theme(theme);
        editor.render_buffer(buf, area);
    }
    pub fn cursor(&self, area: Rect) -> Option<(u16, u16)> {
        self.0.borrow().cursor(area)
    }
    pub fn selection(&self) -> Option<String> {
        self.0.borrow_mut().editor.get_selection_text()
    }
    pub fn find(&self, query: &str, case_sensitive: bool) -> Vec<(usize, usize)> {
        if query.is_empty() {
            return Vec::new();
        }
        let text = self.text();
        let hay = if case_sensitive {
            text.clone()
        } else {
            text.to_ascii_lowercase()
        };
        let needle = if case_sensitive {
            query.to_owned()
        } else {
            query.to_ascii_lowercase()
        };
        hay.match_indices(&needle)
            .map(|(start, found)| {
                let from = text[..start].chars().count();
                (from, from + found.chars().count())
            })
            .collect()
    }
    pub fn mark(&self, ranges: &[(usize, usize)]) {
        self.0
            .borrow_mut()
            .editor
            .set_marks(ranges.iter().map(|&(a, b)| (a, b, "#665900")).collect());
    }
    pub fn clear_marks(&self) {
        self.0.borrow_mut().editor.remove_marks();
    }
    pub fn select(&self, start: usize, end: usize) {
        self.0.borrow_mut().select(start, end);
    }
    pub fn replace(&self, start: usize, end: usize, text: &str) {
        let mut editor = self.0.borrow_mut();
        editor.select(start, end);
        editor.editor.apply(InsertText {
            text: text.to_owned(),
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn render(editor: &mut CodeEditor, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| editor.render(frame, frame.area()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn maps_paths_to_the_vendored_languages() {
        assert_eq!(language_for_path("src/main.rs"), "rust");
        assert_eq!(language_for_path("a/b/README.md"), "markdown");
        assert_eq!(language_for_path("Dockerfile"), "text");
        assert_eq!(language_for_path("script.sh"), "shell");
        assert_eq!(language_for_path("x.unknown"), "text");
    }

    #[test]
    fn renders_a_real_file_with_line_numbers_and_highlighting() {
        let source = "fn main() {\n    let answer = 42;\n    println!(\"{answer}\");\n}\n";
        let mut editor = CodeEditor::new("main.rs", source, false).unwrap();
        let screen = render(&mut editor, 60, 10);
        assert!(screen.contains("fn main() {"), "{screen}");
        assert!(screen.contains("let answer = 42;"), "{screen}");
        assert!(screen.lines().next().unwrap().trim_start().starts_with('1'));
        assert_eq!(editor.text(), source);
    }

    #[test]
    fn renders_one_mebibyte() {
        let line = "let value = compute(alpha, beta, gamma); // a line of source\n";
        let source = line.repeat(1024 * 1024 / line.len());
        let mut editor = CodeEditor::new("big.rs", &source, false).unwrap();
        let screen = render(&mut editor, 100, 40);
        assert!(screen.contains("let value = compute"), "{screen}");
        assert_eq!(editor.text().len(), source.len());
    }

    #[test]
    fn typing_undo_and_movement_reach_the_rope() {
        let mut editor = CodeEditor::new("main.rs", "one two\n", false).unwrap();
        let area = Rect::new(0, 0, 60, 10);
        for c in "abc".chars() {
            assert!(editor.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE), area));
        }
        assert_eq!(editor.text(), "abcone two\n");
        // Home goes to the first non-blank, then to the line start.
        editor.key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE), area);
        editor.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE), area);
        assert!(editor.key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE), area));
        assert_eq!(editor.text(), "bcone two\n");
        editor.key(
            KeyEvent::new(KeyCode::Char('z'), KeyModifiers::CONTROL),
            area,
        );
        assert_eq!(editor.text(), "abcone two\n");
    }

    #[test]
    fn end_inserts_after_the_last_character_with_or_without_a_newline() {
        let area = Rect::new(0, 0, 60, 10);
        for (source, expected) in [
            ("abc\n", "abc!\n"),
            ("abc", "abc!"),
            ("界é\n", "界é!\n"),
            ("\n", "!\n"),
        ] {
            let mut editor = CodeEditor::new("test.txt", source, false).unwrap();
            editor.key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE), area);
            editor.key(KeyEvent::new(KeyCode::Char('!'), KeyModifiers::NONE), area);
            assert_eq!(editor.text(), expected);
        }
    }

    #[test]
    fn one_undo_takes_back_a_typing_run() {
        let mut editor = CodeEditor::new("main.rs", "", false).unwrap();
        let area = Rect::new(0, 0, 60, 10);
        for c in "hello".chars() {
            editor.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE), area);
        }
        assert_eq!(editor.text(), "hello");
        editor.key(
            KeyEvent::new(KeyCode::Char('z'), KeyModifiers::CONTROL),
            area,
        );
        assert_eq!(editor.text(), "");
    }

    #[test]
    fn theme_tokens_drive_the_editor_palette() {
        use crate::theme::Options;
        let theme = Theme::new(Options::default());
        let styles = styles_from_theme(&theme);
        assert_eq!(
            styles["keyword"].fg,
            theme.color("interactive.accent"),
            "keywords use the accent"
        );
        assert_eq!(
            styles["line_number"].fg,
            theme.color("text.muted"),
            "line numbers are muted"
        );
        assert_eq!(styles["selection"].bg, theme.color("interactive.selection"));
        assert_eq!(styles["diff_added"].bg, theme.color("diff.add_bg"));
    }
}
