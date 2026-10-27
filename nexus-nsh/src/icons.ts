// Stroke icons (Lucide, ISC licence), one 24px family. Tinted with a theme token at use.
import panel_left from "./icons/panel-left.svg";
import panel_right from "./icons/panel-right.svg";
import sessions from "./icons/sessions.svg";
import files from "./icons/files.svg";
import projects from "./icons/projects.svg";
import settings from "./icons/settings.svg";
import plus from "./icons/plus.svg";
import search from "./icons/search.svg";
import more from "./icons/more.svg";
import x from "./icons/x.svg";
import chevron_right from "./icons/chevron-right.svg";
import chevron_down from "./icons/chevron-down.svg";
import arrow_up from "./icons/arrow-up.svg";
import stop from "./icons/stop.svg";
import play from "./icons/play.svg";
import bell from "./icons/bell.svg";
import branch from "./icons/branch.svg";
import info from "./icons/info.svg";
import archive from "./icons/archive.svg";
import trash from "./icons/trash.svg";
import pencil from "./icons/pencil.svg";
import copy from "./icons/copy.svg";
import check from "./icons/check.svg";
import alert from "./icons/alert.svg";
import command from "./icons/command.svg";
export const icons = {
  "panel-left": panel_left,
  "panel-right": panel_right,
  "sessions": sessions,
  "files": files,
  "projects": projects,
  "settings": settings,
  "plus": plus,
  "search": search,
  "more": more,
  "x": x,
  "chevron-right": chevron_right,
  "chevron-down": chevron_down,
  "arrow-up": arrow_up,
  "stop": stop,
  "play": play,
  "bell": bell,
  "branch": branch,
  "info": info,
  "archive": archive,
  "trash": trash,
  "pencil": pencil,
  "copy": copy,
  "check": check,
  "alert": alert,
  "command": command,
} as const;
export type IconName = keyof typeof icons;
