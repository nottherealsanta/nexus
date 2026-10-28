// Mock-up theme switch: ?theme=light sets data-theme="light" on <html>; default dark.
(function () {
  var m = /[?&]theme=(light|dark)/.exec(location.search);
  document.documentElement.setAttribute("data-theme", m ? m[1] : "dark");
})();
