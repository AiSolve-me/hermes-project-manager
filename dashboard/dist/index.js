(function() {
  "use strict";

  var MAX_WAIT = 5000;
  var INTERVAL = 50;
  var elapsed = 0;

  var API_BASE = "/api/plugins/project-manager";

  function apiFetch(endpoint, options) {
    options = options || {};
    options.headers = options.headers || {};
    var token = window.__HERMES_SESSION_TOKEN__ || "";
    if (token) {
      options.headers["X-Hermes-Session-Token"] = token;
    }
    if (["POST", "PUT", "PATCH"].indexOf(options.method || "GET") !== -1) {
      if (!options.headers["Content-Type"]) {
        options.headers["Content-Type"] = "application/json";
      }
    }
    return fetch(API_BASE + endpoint, options).then(function(res) {
      if (!res.ok) {
        // Parse the error body so callers get the backend's message
        // (e.g. "Cannot checkout: uncommitted changes...") and machine
        // code (e.g. "WORKTREE_DIRTY") instead of just "HTTP 400".
        // The worktree checkout flow uses the message to detect a
        // dirty worktree and surface a Stash/Force/Cancel dialog — a
        // bare "HTTP 400" would skip that branch and only show a
        // generic "Checkout failed" alert.
        return res.json().then(function(body) {
          var detail = (body && (body.detail || body.error || body.message))
            ? (body.detail || body.error || body.message)
            : "HTTP " + res.status;
          var err = new Error(detail);
          err.status = res.status;
          if (body && body.code) err.code = body.code;
          throw err;
        }, function() {
          // body wasn't JSON — fall back to status code
          throw new Error("HTTP " + res.status);
        });
      }
      return res.json();
    });
  }

  function LoadingState() {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var Card = SDK.components.Card;
    var CardContent = SDK.components.CardContent;

    return h(Card, { className: "mt-4" },
      h(CardContent, { className: "p-6 text-center text-muted-foreground" },
        "Loading projects..."
      )
    );
  }

  function EmptyState() {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var Card = SDK.components.Card;
    var CardContent = SDK.components.CardContent;

    return h(Card, { className: "mt-4" },
      h(CardContent, { className: "p-6 text-center text-muted-foreground" },
        "No projects found."
      )
    );
  }

  function ErrorState(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var Card = SDK.components.Card;
    var CardContent = SDK.components.CardContent;

    return h(Card, { className: "mt-4 border-destructive" },
      h(CardContent, { className: "p-6 text-center text-destructive" },
        props.message || "Failed to load projects."
      )
    );
  }

  function Toast(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var message = props.message;
    var type = props.type || "info";
    var onClose = props.onClose;
    if (!message) return null;
    var icons = { success: "\u2713", error: "\u2715", warning: "\u26a0", info: "\u2139" };
    var colors = {
      success: "bg-green-50 text-green-700 border-green-200",
      error: "bg-red-50 text-red-700 border-red-200",
      warning: "bg-yellow-50 text-yellow-700 border-yellow-200",
      info: "bg-blue-50 text-blue-700 border-blue-200"
    };
    return h("div", {
      className: "fixed top-16 right-4 z-[9999] max-w-sm max-h-32 overflow-y-auto px-4 py-3 rounded-md border shadow-lg flex items-center gap-2 break-words " + (colors[type] || colors.info)
    }, [
      h("span", { key: "icon", className: "font-bold" }, icons[type] || icons.info),
      h("span", { key: "msg", className: "text-sm" }, message),
      onClose ? h("button", {
        key: "close",
        className: "ml-2 font-bold hover:opacity-70",
        onClick: onClose
      }, "\u00d7") : null
    ]);
  }

  function CommitModal(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var Button = SDK.components.Button;
    var Input = SDK.components.Input;
    var state = useState({ message: "", push: true, submitting: false, error: null });
    var data = state[0];
    var setData = state[1];

    function submit(e) {
      e.preventDefault();
      var message = data.message.trim();
      if (!message || data.submitting) return;
      setData({ message: data.message, push: data.push, submitting: true, error: null });
      props.onSubmit(message, data.push).catch(function(err) {
        setData({ message: data.message, push: data.push, submitting: false, error: err.message || "Commit failed" });
      });
    }

    return h("div", { className: "fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4", onClick: data.submitting ? null : props.onClose },
      h("div", { className: "bg-background border border-border rounded-md shadow-lg w-full max-w-md p-5", role: "dialog", "aria-modal": "true", "aria-labelledby": "commit-title", onClick: function(e) { e.stopPropagation(); } },
        h("h2", { id: "commit-title", className: "text-lg font-semibold mb-4" }, "Commit changes"),
        h("form", { onSubmit: submit, className: "space-y-4" },
          h("div", null,
            h("label", { htmlFor: "commit-message", className: "block text-sm font-medium mb-1" }, "Commit message"),
            h(Input, { id: "commit-message", value: data.message, autoFocus: true, disabled: data.submitting, onChange: function(e) { setData({ message: e.target.value, push: data.push, submitting: false, error: null }); } })
          ),
          h("fieldset", { className: "space-y-2" },
            h("legend", { className: "sr-only" }, "Commit action"),
            h("label", { className: "flex items-center gap-2 text-sm" }, h("input", { type: "radio", name: "commit-action", checked: !data.push, disabled: data.submitting, onChange: function() { setData({ message: data.message, push: false, submitting: false, error: null }); } }), "Commit only"),
            h("label", { className: "flex items-center gap-2 text-sm" }, h("input", { type: "radio", name: "commit-action", checked: data.push, disabled: data.submitting, onChange: function() { setData({ message: data.message, push: true, submitting: false, error: null }); } }), "Commit + Push")
          ),
          data.error ? h("p", { className: "text-sm text-destructive", role: "alert" }, data.error) : null,
          h("div", { className: "flex justify-end gap-2" },
            h(Button, { type: "button", variant: "outline", disabled: data.submitting, onClick: props.onClose }, "Cancel"),
            h(Button, { type: "submit", disabled: !data.message.trim() || data.submitting }, data.submitting ? "Committing..." : "OK")
          )
        )
      )
    );
  }

  function GitStatusPanel(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var useEffect = SDK.hooks.useEffect;
    var Badge = SDK.components.Badge;
    var Button = SDK.components.Button;
    var Input = SDK.components.Input;
    // Panel starts active and shows the action row from the props-derived
    // (ahead, behind, isDirty) values. It does NOT hit /git-status on
    // mount — that call is deferred until the user opens the commit
    // modal, which is the only surface that needs the detailed file list
    // (modified_files / untracked_files). The Pull / Push / Commit
    // buttons work from the props immediately, without a network call.
    var state = useState({ status: null, loading: false, error: null, busy: false, modalOpen: false, commitMessage: "", commitPush: true, stashing: false, fetching: false, fileBusy: null, diff: null });
    var data = state[0];
    var setData = state[1];
    var endpoint = "/projects/" + encodeURIComponent(props.projectName);

    // Every setData in this component goes through patch(). The panel's
    // state grew (fetching / fileBusy / diff) and the hand-written
    // per-key copies silently dropped whatever key they forgot to
    // re-list, which wipes new state to undefined on an unrelated
    // update. One merge helper removes that whole failure mode.
    function patch(next) {
      setData(function(prev) {
        var merged = {};
        for (var k in prev) merged[k] = prev[k];
        for (var k2 in next) merged[k2] = next[k2];
        return merged;
      });
    }

    // Single definition of "the commit modal is closed and clean" so the
    // overlay click, Cancel and post-commit paths can't drift apart.
    function modalReset() {
      return { modalOpen: false, loading: false, error: null, busy: false, commitMessage: "", commitPush: true, stashing: false };
    }

    function alertError(message) {
      if (SDK.dialogs && typeof SDK.dialogs.alert === "function") SDK.dialogs.alert({ title: "Git operation failed", description: message });
    }

    // Normalise the two /git-status file shapes (modified_files entries
    // are {path, index_status, worktree_status}, untracked_files entries
    // are {path}) once, at component scope, so both the list rows and the
    // action handlers below agree on path / badge / index state.
    function filePath(file) { return typeof file === "string" ? file : (file.path || file.file || ""); }
    function fileStatus(file) {
      if (typeof file === "string") return "M";
      var code = file.status || (file.index_status && file.index_status !== " " ? file.index_status : file.worktree_status) || "M";
      return String(code).charAt(0).toUpperCase();
    }
    function hasIndexChange(file) {
      if (typeof file === "string") return false;
      return !!(file.index_status && file.index_status !== " ");
    }

    // Reload the per-file status. Shared by openCommitModal and every
    // mutating per-file op so the modal's list always reflects the new
    // index / worktree state after a stage / unstage / discard.
    function refreshStatus() {
      patch({ loading: true, error: null });
      return apiFetch(endpoint + "/git-status").then(function(status) {
        patch({ status: status, loading: false, error: null });
      }).catch(function(err) {
        var message = err.message || "Failed to load Git status";
        patch({ loading: false, error: message });
        alertError(message);
      });
    }

    // Invalidate the backend project cache so the next /projects read
    // returns the post-op git status (isDirty / ahead / behind update).
    // Refresh FIRST, then onChanged, because the parent's loadProjects
    // reads that cache. If refresh fails, log and still call onChanged
    // so the UI updates with whatever the backend does return.
    function refreshProjects() {
      return apiFetch("/projects/refresh", { method: "POST" })
        .then(function() { return true; })
        .catch(function(refreshErr) {
          if (typeof console !== "undefined" && console.warn) console.warn("project-manager: /projects/refresh failed", refreshErr);
          return false;
        })
        .then(function() {
          if (typeof props.onChanged === "function") props.onChanged();
        });
    }

    // The /git-status endpoint exists primarily so the commit modal can
    // show the file list before the user types a message. The Pull /
    // Push / Commit buttons themselves don't need it — they read the
    // ahead/behind/isDirty from the props (which come from the paginated
    // /projects response, refreshed on every /projects/refresh).

    function run(path, body, successMessage) {
      patch({ error: null, busy: true });
      return apiFetch(endpoint + path, { method: "POST", body: JSON.stringify(body) }).then(function() {
        if (SDK.dialogs && typeof SDK.dialogs.alert === "function") SDK.dialogs.alert({ title: "Git", description: successMessage });
        patch(modalReset());
        return refreshProjects();
      }).catch(function(err) {
        patch({ error: err.message, busy: false });
        alertError(err.message || "Git operation failed");
        throw err;
      });
    }

    function openCommitModal() {
      // Fetch the file list right before showing the modal so the user
      // sees an up-to-date list of what's about to be committed. Reset
      // commitMessage/commitPush/stashing so a stale value from the
      // previous open doesn't leak into the new one.
      patch({ modalOpen: true, loading: true, error: null, busy: false, commitMessage: "", commitPush: true, stashing: false });
      refreshStatus();
    }

    // Fetch from origin without touching the working tree. Never writes
    // modalOpen, so it is safe to hit while the commit modal is open.
    function doFetch() {
      if (data.fetching || data.busy) return;
      patch({ fetching: true, error: null });
      apiFetch(endpoint + "/git/fetch", { method: "POST", body: JSON.stringify({ remote: "origin" }) })
        .then(function(result) {
          patch({ fetching: false });
          // apiFetch only rejects on transport / HTTP errors — a git
          // failure comes back 200 + success:false, and treating that
          // resolved promise as success would report a no-op as done.
          if (!result || !result.success) {
            var failure = (result && result.message) ? result.message : "Fetch failed";
            patch({ error: failure });
            alertError(failure);
            return;
          }
          var message = result.message || "Fetched origin.";
          if (SDK.dialogs && typeof SDK.dialogs.alert === "function") SDK.dialogs.alert({ title: "Git", description: message });
          // Fetch doesn't touch the tree but it does move the remote
          // refs, so behind/ahead can change — refresh the same way the
          // other mutating ops do.
          refreshProjects();
        })
        .catch(function(err) {
          patch({ fetching: false });
          var message = err.message || "Fetch failed";
          patch({ error: message });
          alertError(message);
        });
    }

    // Per-file stage / unstage / discard. Unlike run() this keeps the
    // commit modal open — the file list is the surface the user is
    // working in, and reloading it in place is the feedback.
    function runFileOp(action, file) {
      var path = filePath(file);
      if (!path || data.busy || data.fileBusy) return;
      patch({ fileBusy: path, error: null });
      apiFetch(endpoint + "/git/" + action, { method: "POST", body: JSON.stringify({ filepath: path }) })
        .then(function(result) {
          patch({ fileBusy: null });
          // A path that escapes the repo, or a git failure, returns
          // 200 + success:false. Check the flag or the row silently
          // stays unchanged and the user thinks it worked.
          if (!result || !result.success) {
            var failure = (result && result.message) ? result.message : ("Git " + action + " failed");
            patch({ error: failure });
            alertError(failure);
            return;
          }
          return refreshStatus().then(function() {
            return refreshProjects();
          });
        })
        .catch(function(err) {
          patch({ fileBusy: null });
          var message = err.message || ("Git " + action + " failed");
          patch({ error: message });
          alertError(message);
        });
    }

    // Discard is irreversible and behaves differently for tracked vs
    // untracked files, so the confirm text has to spell out which.
    function handleDiscard(file, untracked) {
      var path = filePath(file);
      if (!path) return;
      var perform = function() { runFileOp("discard", file); };
      if (SDK && SDK.dialogs && typeof SDK.dialogs.confirm === "function") {
        SDK.dialogs.confirm({
          title: "Discard changes?",
          description: untracked
            ? "'" + path + "' is untracked. Discarding DELETES it from disk — it is in no commit, so git cannot recover it."
            : "'" + path + "' is tracked. Discarding restores it from HEAD — uncommitted local edits to it are lost forever.",
          destructive: true,
          confirmLabel: "Discard",
          cancelLabel: "Cancel",
          onConfirm: perform,
          onCancel: function() {}
        });
        return;
      }
      if (window.confirm("Discard changes to " + path + "? " + (untracked
        ? "The file will be DELETED from disk."
        : "Uncommitted local edits will be lost forever."))) {
        perform();
      }
    }

    // Stash-all is reachable from the SDK confirm dialog and from the
    // window.confirm fallback, so the body lives in one place instead of
    // being copy-pasted into both branches.
    function performStash() {
      patch({ stashing: true, error: null });
      apiFetch(endpoint + "/git-stash", { method: "POST", body: JSON.stringify({}) })
        .then(function() {
          if (typeof props.onChanged === "function") props.onChanged();
          patch(modalReset());
        })
        .catch(function(err) {
          var msg = (err && err.message) ? err.message : "Stash failed";
          patch({ error: msg, stashing: false });
          alertError(msg);
        });
    }

    function openDiff(file) {
      var path = filePath(file);
      if (!path) return;
      patch({ diff: { path: path, loading: true, data: null, error: null } });
      apiFetch(endpoint + "/git/diff?file=" + encodeURIComponent(path))
        .then(function(result) {
          setDiff(path, function() { return { path: path, loading: false, data: result, error: null }; });
        })
        .catch(function(err) {
          setDiff(path, function() { return { path: path, loading: false, data: null, error: err.message || "Failed to load diff" }; });
        });
    }

    // Drop a late /git/diff response when the viewer was closed (or moved
    // to another file) while the request was still in flight.
    function setDiff(path, factory) {
      setData(function(prev) {
        if (!prev.diff || prev.diff.path !== path) return prev;
        var merged = {};
        for (var k in prev) merged[k] = prev[k];
        merged.diff = factory();
        return merged;
      });
    }

    function closeDiff() { patch({ diff: null }); }

    // Escape closes the diff viewer. The listener only exists while the
    // viewer is on screen, so it can't swallow keys elsewhere on the page.
    useEffect(function() {
      if (!data.diff) return;
      function onKeyDown(e) {
        if (e.key === "Escape") closeDiff();
      }
      window.addEventListener("keydown", onKeyDown);
      return function() { window.removeEventListener("keydown", onKeyDown); };
    }, [data.diff]);

    var aheadCount = props.ahead || 0;
    var behindCount = props.behind || 0;
    var isDirty = !!props.isDirty;

    // Action row — all three buttons read from props. The Pull button
    // delegates to props.onPull (which fires /git-pull, server-side
    // validation handles dirty state). The Push button only renders
    // when the branch is ahead. The Commit button only renders when the
    // working tree is dirty, and opens the modal which loads the file
    // list via /git-status.
    var actionRow = h("div", { className: "flex flex-wrap items-center gap-1.5" },
      h(Button, {
        size: "sm",
        variant: "outline",
        disabled: !!props.pulling || isDirty,
        onClick: function(e) { if (props.onPull) props.onPull(e); }
      }, props.pulling ? "Pulling..." : "Pull"),
      aheadCount > 0 ? h(Button, {
        size: "sm",
        variant: "outline",
        disabled: !!data.busy,
        onClick: function() { run("/git-push", { remote: "origin", branch: null }, "Pushed.").catch(function() {}); }
      }, data.busy ? "Working..." : "Push") : null,
      isDirty ? h(Button, {
        size: "sm",
        disabled: !!data.busy,
        onClick: openCommitModal
      }, "Commit + Push") : null,
      h(Button, {
        size: "sm",
        variant: "outline",
        disabled: !!data.busy || !!data.fetching,
        onClick: doFetch,
        title: "Fetch from origin without changing the working tree"
      }, data.fetching ? "Fetching..." : "Fetch")
    );

    if (data.modalOpen) {
      // Render the commit modal — needs the freshly-loaded file list.
      var status = data.status;
      var modified = status ? (status.modified_files || []) : [];
      var untracked = status ? (status.untracked_files || []) : [];
      var dirtyCount = modified.length + untracked.length;
      // One row builder for both lists. Buttons lock while any git op is
      // in flight so a double-click can't queue two conflicting writes.
      function fileRow(file, key, isUntracked) {
        var path = filePath(file);
        var locked = !!data.busy || !!data.fileBusy;
        function action(label, hint, disabled, onClick, destructive) {
          return h(Button, {
            key: label,
            size: "sm",
            variant: "outline",
            disabled: !!disabled,
            title: hint,
            "aria-label": hint,
            className: "shrink-0 text-[11px] px-1.5 " + (destructive ? "text-destructive border-destructive/40 hover:bg-destructive/10" : ""),
            onClick: onClick
          }, label);
        }
        return h("li", { key: key, className: "flex items-center gap-1.5" },
          h(Badge, { variant: "outline", className: "text-[10px] w-6 justify-center shrink-0" }, isUntracked ? "?" : fileStatus(file)),
          h("code", { className: "truncate flex-1 min-w-0", title: path }, path),
          action("Stage", "Stage " + path, locked, function() { runFileOp("stage", file); }),
          action("Unstage", "Unstage " + path, locked || !hasIndexChange(file), function() { runFileOp("unstage", file); }),
          action("Diff", "Show diff for " + path, locked, function() { openDiff(file); }),
          action("Discard", "Discard changes in " + path, locked, function() { handleDiscard(file, isUntracked); }, true)
        );
      }

      // Split a diff into coloured lines. No parser: the git preamble
      // ("diff --git" / "index" / "---" / "+++") only repeats the
      // filename this view is already titled with, so it goes, and the
      // +/- lines get the cheapest possible emphasis.
      function diffLines(text) {
        if (!text) return null;
        var rows = String(text).split("\n");
        var out = [];
        for (var i = 0; i < rows.length; i++) {
          var line = rows[i];
          if (line.indexOf("diff --git ") === 0) continue;
          if (line.indexOf("index ") === 0) continue;
          if (line.indexOf("--- ") === 0) continue;
          if (line.indexOf("+++ ") === 0) continue;
          var cls = "";
          if (line.charAt(0) === "+") cls = "text-green-600";
          else if (line.charAt(0) === "-") cls = "text-destructive";
          else if (line.charAt(0) === "@") cls = "text-muted-foreground";
          out.push(h("span", { key: "l" + i, className: cls }, line + "\n"));
        }
        return out.length > 0 ? out : null;
      }

      function diffSection(title, text) {
        var body = diffLines(text) || h("span", { className: "text-muted-foreground" }, "No changes.");
        return h("div", { className: "space-y-1" },
          h("div", { className: "text-xs font-semibold text-muted-foreground" }, title),
          h("pre", { className: "text-[11px] font-mono bg-muted border border-border rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap" }, body)
        );
      }

      // Nested overlay, not a takeover — the file list stays mounted and
      // visible behind it, so Diff is a peek, not a navigation.
      function diffOverlay() {
        var diff = data.diff;
        if (!diff) return null;
        var payload = diff.data;
        var content = diff.loading ? h("p", { className: "text-sm text-muted-foreground" }, "Loading diff...")
          : diff.error ? h("p", { className: "text-sm text-destructive", role: "alert" }, diff.error)
          : (!payload || !payload.success) ? h("p", { className: "text-sm text-destructive", role: "alert" }, (payload && payload.message) ? payload.message : "Failed to load diff.")
          : payload.binary ? h("p", { className: "text-sm text-muted-foreground" }, "Binary file — git produced no textual diff.")
          : h("div", { className: "space-y-3" },
              payload.truncated ? h("p", { className: "text-xs text-destructive", role: "alert" }, "Diff truncated — this file is larger than the server's diff limit.") : null,
              diffSection("Staged", payload.staged_diff),
              diffSection("Working tree", payload.worktree_diff)
            );
        return h("div", { className: "fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4", onClick: closeDiff },
          h("div", { className: "bg-background border border-border rounded-md shadow-lg w-full max-w-2xl p-5", role: "dialog", "aria-modal": "true", "aria-labelledby": "diff-title", onClick: function(e) { e.stopPropagation(); } },
            h("div", { className: "flex items-center justify-between gap-2 mb-3" },
              h("h3", { id: "diff-title", className: "text-base font-semibold truncate", title: diff.path }, diff.path),
              h("button", {
                type: "button",
                className: "text-muted-foreground hover:text-foreground text-xl leading-none",
                onClick: closeDiff,
                "aria-label": "Close diff"
              }, "\u2715")
            ),
            content
          )
        );
      }

      var commitModal = h("div", { className: "fixed inset-0 z-50 flex items-center justify-center bg-black/40", onClick: function(e) { if (e.target === e.currentTarget && !data.busy && !data.stashing) patch(modalReset()); } },
        h("div", { className: "bg-background border border-border rounded-md shadow-lg w-full max-w-md p-5 m-4", role: "dialog", "aria-modal": "true", "aria-labelledby": "commit-title", onClick: function(e) { e.stopPropagation(); } },
          h("h3", { id: "commit-title", className: "text-base font-semibold mb-3" }, "Commit changes"),
          data.loading ? h("p", { className: "text-sm text-muted-foreground" }, "Loading file list...") :
          data.error ? h("p", { className: "text-sm text-destructive", role: "alert" }, data.error) :
          h("div", null,
            h("p", { className: "text-xs text-muted-foreground mb-2" }, dirtyCount + " file(s) to commit:"),
            h("ul", { className: "text-xs max-h-48 overflow-y-auto mb-3 space-y-1" },
              modified.map(function(file, i) { return fileRow(file, "m" + i, false); }),
              untracked.map(function(file, i) { return fileRow(file, "u" + i, true); })
            )
          ),
          h("label", { htmlFor: "commit-message", className: "block text-sm font-medium mb-1" }, "Commit message"),
          h(Input, { id: "commit-message", autoFocus: true, disabled: data.busy || data.stashing, value: data.commitMessage, onChange: function(e) { patch({ error: null, commitMessage: e.target.value }); }, placeholder: "fix: ..." }),
          h("div", { className: "flex items-center gap-2 mt-3" },
            h("input", { type: "checkbox", id: "commit-push", checked: data.commitPush, onChange: function(e) { patch({ error: null, commitPush: e.target.checked }); } }),
            h("label", { htmlFor: "commit-push", className: "text-sm" }, "Push after commit")
          ),
          h("div", { className: "flex justify-between gap-2 mt-4" },
            h(Button, {
              variant: "outline",
              disabled: data.busy || data.stashing,
              onClick: function() {
                if (SDK && SDK.dialogs && typeof SDK.dialogs.confirm === "function") {
                  SDK.dialogs.confirm({
                    title: "Stash all changes?",
                    description: "This will run `git stash push -u` on this project, moving all " + dirtyCount + " file(s) (including untracked) to the stash. The stash entry is preserved on the stack so you can `git stash pop` later.\n\nThis cannot be undone.",
                    destructive: true,
                    confirmLabel: "Stash all",
                    cancelLabel: "Cancel",
                    onConfirm: performStash,
                    onCancel: function() {}
                  });
                } else if (window.confirm("Stash all changes? This cannot be undone.")) {
                  performStash();
                }
              },
              className: "text-destructive border-destructive/40 hover:bg-destructive/10"
            }, data.stashing ? "Stashing..." : "Stash all"),
            h("div", { className: "flex gap-2" },
              h(Button, { variant: "outline", disabled: data.busy || data.stashing, onClick: function() { patch(modalReset()); } }, "Cancel"),
              h(Button, { disabled: data.busy || data.stashing || !(data.commitMessage || "").trim(), onClick: function() {
                if (!(data.commitMessage || "").trim()) return;
                run("/git-commit", { message: data.commitMessage, commit_and_push: data.commitPush }, "Committed: " + data.commitMessage);
              } }, data.busy ? "Working..." : "Commit")
            )
          )
        )
      );
      return h("div", null, commitModal, diffOverlay());
    }

    return h("section", { className: "mt-2 border-t border-border pt-2 space-y-2", "aria-label": "Working tree" },
      actionRow
    );
  }

  function ProjectCard(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var Card = SDK.components.Card;
    var CardHeader = SDK.components.CardHeader;
    var CardTitle = SDK.components.CardTitle;
    var CardContent = SDK.components.CardContent;
    var Badge = SDK.components.Badge;
    var Button = SDK.components.Button;
    var project = props.project;
    var onSync = props.onSync;
    var onToast = props.onToast;

    var copyState = useState({ copied: false, branchCopied: null });
    var copied = copyState[0].copied;
    var branchCopied = copyState[0].branchCopied;
    var setCopyState = copyState[1];

    var syncState = useState({ pulling: false, pullResult: null });
    var pulling = syncState[0].pulling;
    var pullResult = syncState[0].pullResult;
    var setPullState = syncState[1];

    function truncateCommit(msg, maxLen) {
      maxLen = maxLen || 60;
      if (!msg) return "";
      return msg.length > maxLen ? msg.slice(0, maxLen) + "..." : msg;
    }

    function timeAgo(dateStr) {
      if (!dateStr) return null;
      var date = new Date(dateStr);
      if (isNaN(date.getTime())) return null;
      var now = new Date();
      var diffMs = now - date;
      var diffSec = Math.floor(diffMs / 1000);
      if (diffSec < 60) return diffSec + "s ago";
      var diffMin = Math.floor(diffSec / 60);
      if (diffMin < 60) return diffMin + "m ago";
      var diffHr = Math.floor(diffMin / 60);
      if (diffHr < 24) return diffHr + "h ago";
      var diffDay = Math.floor(diffHr / 24);
      if (diffDay < 30) return diffDay + "d ago";
      var diffMo = Math.floor(diffDay / 30);
      if (diffMo < 12) return diffMo + "mo ago";
      return Math.floor(diffDay / 365) + "y ago";
    }

    function copyPath(e) {
      e.stopPropagation();
      if (project.path && navigator.clipboard) {
        navigator.clipboard.writeText(project.path).then(function() {
          setCopyState({ copied: true, branchCopied: null });
          setTimeout(function() { setCopyState({ copied: false, branchCopied: null }); }, 1500);
        });
      }
    }

    function handlePull(e) {
      e.stopPropagation();
      if (pulling) return;
      setPullState({ pulling: true, pullResult: null });
      apiFetch("/projects/" + encodeURIComponent(project.name) + "/git-pull", { method: "POST", body: JSON.stringify({}) })
        .then(function(result) {
          setPullState({ pulling: false, pullResult: result });
          // Only re-fetch the project list when new commits actually
          // landed. A no-op pull ("Already up to date.") sets
          // result.pulled = false, and a stale-data re-render would
          // just re-paint the same last_commit / ahead / behind values
          // we already have. The re-scan + cache invalidation on the
          // server is also skipped in that case, so the cache key
          // remains warm and the next /projects read returns the
          // exact same data the UI already has.
          if (result && result.success && result.pulled && typeof onSync === "function") {
            onSync();
          }
          if (typeof onToast === "function") {
            if (result && result.success) {
              onToast({ message: result.message || "Pull successful.", type: "success" });
            } else {
              onToast({ message: result.message || "Pull failed", type: "error" });
            }
          }
          setTimeout(function() { setPullState({ pulling: false, pullResult: null }); }, 4000);
        })
        .catch(function(err) {
          setPullState({ pulling: false, pullResult: { success: false, message: err.message || "Pull failed" } });
          if (typeof onToast === "function") {
            onToast({ message: err.message || "Pull failed", type: "error" });
          }
          setTimeout(function() { setPullState({ pulling: false, pullResult: null }); }, 4000);
        });
    }

    function normalizeRemoteUrl(url) {
      if (!url) return null;
      // Convert SSH URL to HTTPS: git@host:user/repo.git → https://host/user/repo.git
      if (url.indexOf("git@") === 0) {
        url = url.replace(/^git@([^:]+):/, "https://$1/");
      }
      // Only allow http:// or https:// URLs
      if (url.indexOf("http://") !== 0 && url.indexOf("https://") !== 0) {
        return null;
      }
      return url.replace(/\.git$/, "");
    }

    function openBranch(e, branch) {
      e.stopPropagation();
      var url = normalizeRemoteUrl(project.remote_url);
      if (!url || !branch) return;
      var branchUrl;
      if (url.indexOf("github.com") !== -1) {
        branchUrl = url + "/tree/" + encodeURIComponent(branch);
      } else if (url.indexOf("gitlab.com") !== -1) {
        branchUrl = url + "/-/tree/" + encodeURIComponent(branch);
      } else if (url.indexOf("bitbucket.org") !== -1) {
        branchUrl = url + "/src/" + encodeURIComponent(branch);
      } else {
        // Generic: try /tree/branch
        branchUrl = url + "/tree/" + encodeURIComponent(branch);
      }
      window.open(branchUrl, "_blank");
    }

    function openGit(e) {
      e.stopPropagation();
      var url = normalizeRemoteUrl(project.remote_url);
      if (url) {
        window.open(url, "_blank");
      }
    }

    var commitTime = timeAgo(project.last_commit_date);
    var pushTime = project.last_push_date ? timeAgo(project.last_push_date) : null;
    var branchCount = project.branch_count || 0;
    var currentBranch = project.current_branch || "";
    var branches = project.branches || [];
    var aheadCount = project.ahead || 0;
    var behindCount = project.behind || 0;

    var pullBadgeClass = pullResult && pullResult.success ? "text-green-600" : (pullResult ? "text-destructive" : "");
    var pullBadgeText = pullResult ? pullResult.message : "";

    return h(Card, { className: "hover:shadow-md transition-shadow h-full" },
      h(CardHeader, { className: "pb-1 px-3 pt-3" },
        h("div", { className: "flex items-center justify-between gap-2" },
          h(CardTitle, {
            className: "text-sm font-semibold truncate cursor-pointer hover:text-blue-600 dark:hover:text-blue-400 transition-colors",
            onClick: openGit,
            title: project.remote_url ? "Click to open Git repository" : "No remote URL"
          }, project.name || "Unnamed project"),
          h("div", { className: "flex items-center gap-2 shrink-0" },
            currentBranch ? h(Badge, { variant: "outline", className: "text-xs font-mono" },
              h("svg", { width: 12, height: 12, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", className: "mr-1 inline-block" }, h("path", { d: "M6 3v12" }), h("circle", { cx: "18", cy: "6", r: 3 }), h("circle", { cx: "6", cy: "18", r: 3 }), h("path", { d: "M18 9a9 9 0 0 1-9 9" })),
              currentBranch
            ) : null,
            branchCount > 1 ? h(Badge, { variant: "secondary", className: "text-xs" }, "+" + (branchCount - 1)) : null,
            project.has_docker ? h(Badge, {
              className: "text-xs " + (project.docker_running ? "bg-green-100 text-green-700 hover:bg-green-100" : "bg-slate-100 text-slate-600 hover:bg-slate-100"),
              title: project.docker_running
                ? ((project.docker_ports && project.docker_ports.length)
                    ? ("Published ports: " + project.docker_ports.join(", "))
                    : "Compose stack running (no published ports)")
                : "Docker files present, not running"
            }, project.docker_running
              ? ("Docker" + ((project.docker_ports && project.docker_ports.length)
                  ? (" :" + (project.docker_ports.length <= 3
                      ? project.docker_ports.join(" :")
                      : project.docker_ports.slice(0, 2).join(" :") + " +" + (project.docker_ports.length - 2)))
                  : ""))
              : "Docker off") : null,
            project.is_dirty ? h("span", {
              className: "relative flex h-3 w-3 shrink-0",
              title: "Uncommitted changes"
            },
              h("span", { className: "animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75" }),
              h("span", { className: "relative inline-flex rounded-full h-3 w-3 bg-red-500" })
            ) : null
          )
        )
      ),
      h(CardContent, { className: "pt-0 px-3 pb-3" },
        h("div", { className: "space-y-1.5" },
          project.path ? h("div", { className: "flex items-center gap-1.5" },
          h("code", {
            className: "text-xs bg-muted text-foreground border border-border px-1.5 py-0.5 rounded cursor-pointer hover:bg-accent hover:text-accent-foreground transition-colors truncate",
            title: "Click to copy path",
            onClick: copyPath
          }, copied ? "\u2713 Copied!" : project.path)
          ) : null,

          // Working tree status row — rendered from project.{ahead,behind,is_dirty}
          // so the badges appear immediately on mount with 0 extra API calls.
          // The detailed file list (modified_files / untracked_files) is only
          // loaded when the user opens the commit modal via the loadStatus()
          // hook inside GitStatusPanel, not on every card mount.
          (aheadCount > 0 || behindCount > 0 || project.is_dirty) ? h("section", { className: "mt-2 border-t border-border pt-2 space-y-2", "aria-label": "Working tree" },
            h("div", { className: "flex flex-wrap items-center gap-1.5" },
              h("span", { className: "text-xs font-semibold mr-1" }, "Working tree"),
              aheadCount > 0 ? h(Badge, { className: "text-xs bg-red-100 text-red-700 hover:bg-red-100" }, "\u2191 " + aheadCount) : null,
              behindCount > 0 ? h(Badge, { className: "text-xs bg-yellow-100 text-yellow-700 hover:bg-yellow-100" }, "\u2193 " + behindCount) : null,
              project.is_dirty ? h(Badge, { className: "text-xs bg-orange-100 text-orange-700 hover:bg-orange-100" }, "Dirty") : null
            )
          ) : null,

          // Action row — Pull (always available when branch tracks upstream)
          // and Commit + Push (only when the working tree is dirty). The Push
          // button is also exposed when the branch is ahead of upstream. All
          // three operate against the git-pull / git-commit / git-push
          // endpoints, which already validate dirty state and upstream
          // presence server-side.
          h(GitStatusPanel, {
            projectName: project.name,
            projectPath: project.path,
            ahead: aheadCount,
            behind: behindCount,
            isDirty: project.is_dirty,
            pulling: pulling,
            onPull: handlePull,
            onChanged: onSync
          }),

          // Per-project worktree + branch picker (collapsible). Was previously
          // rendered once at the top of ProjectManagerPage, which forced the
          // user to manually pick a project. Now every project card has its
          // own toggle so worktree/branch ops are available for every project.
          h(WorktreeBranchPanel, {
            projectName: project.name,
            onChanged: onSync
          }),

          branches.length > 0 ? h("div", { className: "flex flex-wrap gap-1" },
            branches.slice(0, 5).map(function(branch, index) {
              var isCurrent = branch === currentBranch;
              return h(Badge, {
                key: index,
                variant: isCurrent ? "default" : "secondary",
                className: "text-xs cursor-pointer hover:opacity-80 transition-opacity " + (isCurrent ? "font-bold" : ""),
                title: isCurrent ? "Current branch — click to open" : "Click to open branch",
                onClick: function(e) { openBranch(e, branch); }
              }, (isCurrent ? [h("svg", { width: 8, height: 8, viewBox: "0 0 24 24", fill: "currentColor", className: "inline-block mr-1" }, h("circle", { cx: "12", cy: "12", r: "6" })), " ", branch] : branch));
            }),
            branches.length > 5 ? h(Badge, { variant: "outline", className: "text-xs" }, "+" + (branches.length - 5)) : null
          ) : null,

          project.last_commit_message ? h("p", {
            className: "text-xs text-muted-foreground truncate"
          }, [h("svg", { width: 10, height: 10, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, className: "inline-block mr-1" }, h("path", { d: "M12 19V5M5 12l7-7 7 7" })), " ", truncateCommit(project.last_commit_message)]) : null,
          h("div", { className: "flex flex-wrap gap-3 text-xs text-muted-foreground" },
            pushTime ? h("span", { title: project.last_push_date }, [h("svg", { width: 10, height: 10, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, className: "inline-block mr-1" }, h("path", { d: "M12 19V5M5 12l7-7 7 7" })), " ", pushTime]) : null
          )
        )
      )
    );
  }

  function SearchInput(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var Input = SDK.components.Input;

    return h(Input, {
      type: "text",
      placeholder: "Search projects...",
      value: props.value || "",
      onChange: function(e) {
        if (props.onChange) props.onChange(e.target.value);
      },
      className: "w-full"
    });
  }

  function RefreshButton(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var Button = SDK.components.Button;

    var state = useState({ refreshing: false, error: null });
    var data = state[0];
    var setData = state[1];

    function handleClick() {
      if (data.refreshing) return;
      setData({ refreshing: true, error: null });
      apiFetch("/projects/refresh", { method: "POST" })
        .then(function(result) {
          setData({ refreshing: false, error: null });
          if (typeof props.onRefresh === "function") {
            props.onRefresh();
          }
        })
        .catch(function(err) {
          setData({ refreshing: false, error: err.message || "Refresh failed" });
        });
    }

    return h("div", { className: "flex items-center gap-2" },
      h(Button, {
        onClick: handleClick,
        disabled: data.refreshing,
        size: "sm",
        className: data.refreshing ? "opacity-50 cursor-not-allowed" : ""
      }, data.refreshing ? "Refreshing..." : "Refresh"),
      data.error ? h("span", { className: "text-sm text-destructive" }, data.error) : null
 );
 }

 function PullAllButton(props) {
 var SDK = window.__HERMES_PLUGIN_SDK__;
 var React = SDK.React;
 var h = React.createElement;
 var useState = SDK.hooks.useState;
 var Button = SDK.components.Button;

 var state = useState({ pulling: false, error: null });
 var data = state[0];
 var setData = state[1];

 function handleClick() {
 if (data.pulling) return;
 setData({ pulling: true, error: null });
 apiFetch("/projects/pull-all", { method: "POST", body: JSON.stringify({}) })
   .then(function(result) {
     setData({ pulling: false, error: null });
     // Build a concise summary line: "5 pulled, 3 up-to-date, 1 dirty, 1 diverged"
     var parts = [];
     if (result.pulled > 0) parts.push(result.pulled + " pulled");
     if (result.already_up_to_date > 0) parts.push(result.already_up_to_date + " up-to-date");
     if (result.dirty_skipped > 0) parts.push(result.dirty_skipped + " dirty (skipped)");
     if (result.diverged > 0) parts.push(result.diverged + " diverged");
     if (result.failed > 0) parts.push(result.failed + " failed");
     var summary = parts.length > 0 ? parts.join(", ") : (result.total || 0) + " processed";
     if (typeof props.onPullAll === "function") {
       props.onPullAll(result);
     }
     if (typeof props.onToast === "function") {
       var toastType = result.failed > 0 || result.diverged > 0 ? "error" : "success";
       props.onToast({ message: summary, type: toastType });
     }
   })
   .catch(function(err) {
     setData({ pulling: false, error: err.message || "Pull all failed" });
     if (typeof props.onToast === "function") {
       props.onToast({ message: err.message || "Pull all failed", type: "error" });
     }
   });
 }

 return h("div", { className: "flex items-center gap-2" },
 h(Button, {
   onClick: handleClick,
   disabled: data.pulling,
   size: "sm",
   className: data.pulling ? "opacity-50 cursor-not-allowed" : ""
 }, data.pulling ? "Pulling..." : "Pull All"),
 data.error ? h("span", { className: "text-sm text-destructive" }, data.error) : null
 );
 }

  function ProjectList(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var useEffect = SDK.hooks.useEffect;

    var state = useState({
      projects: [],
      loading: true,
      error: null,
      searchQuery: "",
      sortBy: "lastCommit",
      page: 1,
      pageSize: 21,
      total: 0,
      totalFiltered: 0,
      pageCount: 1,
      scannedAt: null
    });
    var data = state[0];
    var setData = state[1];

    var toastState = useState(null);
    var toast = toastState[0];
    var setToast = toastState[1];

    function showToast(toastData) {
      setToast(toastData);
      setTimeout(function() { setToast(null); }, 4000);
    }

    // Build the API query string from the current state.
    function buildQueryString() {
      var qs = "page=" + encodeURIComponent(data.page) +
               "&page_size=" + encodeURIComponent(data.pageSize);
      if (data.searchQuery) {
        qs += "&q=" + encodeURIComponent(data.searchQuery);
      }
      return qs;
    }

    // Always hit the backend so the search runs across the full project set.
    // Backend handles caching and pagination; client just renders the page slice.
    function loadProjects() {
      setData(function(prev) {
        return {
          projects: prev.projects,
          loading: true,
          error: null,
          searchQuery: prev.searchQuery,
          sortBy: prev.sortBy,
          page: prev.page,
          pageSize: prev.pageSize,
          total: prev.total,
          totalFiltered: prev.totalFiltered,
          pageCount: prev.pageCount,
          scannedAt: prev.scannedAt
        };
      });
      apiFetch("/projects?" + buildQueryString())
        .then(function(response) {
          var projects = response && response.projects ? response.projects : [];
          setData(function(prev) {
            return {
              projects: projects,
              loading: false,
              error: null,
              searchQuery: prev.searchQuery,
              sortBy: prev.sortBy,
              page: response.page || prev.page,
              pageSize: response.page_size || prev.pageSize,
              total: response.total != null ? response.total : projects.length,
              totalFiltered: response.total_filtered != null
                ? response.total_filtered
                : (response.total != null ? response.total : projects.length),
              pageCount: response.page_count || 1,
              scannedAt: response.scanned_at || prev.scannedAt
            };
          });
        })
        .catch(function(err) {
          setData(function(prev) {
            return {
              projects: [],
              loading: false,
              error: err.message || "Unknown error",
              searchQuery: prev.searchQuery,
              sortBy: prev.sortBy,
              page: prev.page,
              pageSize: prev.pageSize,
              total: 0,
              totalFiltered: 0,
              pageCount: 1,
              scannedAt: prev.scannedAt
            };
          });
        });
    }

    // Initial load + reload on (page, pageSize, searchQuery) change.
    useEffect(function() {
      loadProjects();
    }, [data.page, data.pageSize, data.searchQuery]);

    function handleSearchChange(query) {
      // New search always resets to page 1.
      setData(function(prev) {
        return {
          projects: prev.projects,
          loading: prev.loading,
          error: prev.error,
          searchQuery: query,
          sortBy: prev.sortBy,
          page: 1,
          pageSize: prev.pageSize,
          total: prev.total,
          totalFiltered: prev.totalFiltered,
          pageCount: prev.pageCount,
          scannedAt: prev.scannedAt
        };
      });
    }

    function handleSortChange(sortBy) {
      setData(function(prev) {
        return {
          projects: prev.projects,
          loading: prev.loading,
          error: prev.error,
          searchQuery: prev.searchQuery,
          sortBy: sortBy,
          page: prev.page,
          pageSize: prev.pageSize,
          total: prev.total,
          totalFiltered: prev.totalFiltered,
          pageCount: prev.pageCount,
          scannedAt: prev.scannedAt
        };
      });
    }

    function setPageSize(size) {
      if (size === data.pageSize) return;
      setData(function(prev) {
        return {
          projects: prev.projects,
          loading: prev.loading,
          error: prev.error,
          searchQuery: prev.searchQuery,
          sortBy: prev.sortBy,
          page: 1,
          pageSize: size,
          total: prev.total,
          totalFiltered: prev.totalFiltered,
          pageCount: prev.pageCount,
          scannedAt: prev.scannedAt
        };
      });
    }

    function goToPage(page) {
      if (page < 1 || page > data.pageCount || page === data.page) return;
      setData(function(prev) {
        return {
          projects: prev.projects,
          loading: prev.loading,
          error: prev.error,
          searchQuery: prev.searchQuery,
          sortBy: prev.sortBy,
          page: page,
          pageSize: prev.pageSize,
          total: prev.total,
          totalFiltered: prev.totalFiltered,
          pageCount: prev.pageCount,
          scannedAt: prev.scannedAt
        };
      });
    }

    // Sort the current page slice in-memory. Backend already orders
    // by watch_path_sort_order, last_commit_date desc, name asc — the
    // sort selector only changes the visible order within the page.
    var sortedProjects = data.projects.slice();
    sortedProjects.sort(function(a, b) {
      if (data.sortBy === "name") {
        return (a.name || "").localeCompare(b.name || "");
      }
      if (data.sortBy === "branchCount") {
        return (b.branch_count || b.branchCount || 0)
             - (a.branch_count || a.branchCount || 0);
      }
      var dateA = a.last_commit_date ? new Date(a.last_commit_date).getTime() : 0;
      var dateB = b.last_commit_date ? new Date(b.last_commit_date).getTime() : 0;
      return dateB - dateA;
    });

    var sortLabel = data.sortBy === "name"
      ? "Name"
      : (data.sortBy === "branchCount" ? "Branch count" : "Last commit");

    // Build the page-number strip. For small page counts show all
    // pages; for larger ones, show first / prev / current window /
    // next / last with ellipses.
    function buildPageNumbers() {
      var current = data.page;
      var last = data.pageCount;
      if (last <= 1) return [];
      if (last <= 7) {
        var arr = [];
        for (var i = 1; i <= last; i++) arr.push(i);
        return arr;
      }
      var pages = [1];
      var start = Math.max(2, current - 1);
      var end = Math.min(last - 1, current + 1);
      if (start > 2) pages.push("...");
      for (var j = start; j <= end; j++) pages.push(j);
      if (end < last - 1) pages.push("...");
      pages.push(last);
      return pages;
    }

    var pageNumbers = buildPageNumbers();
    var pageSizeOptions = [9, 21, 27];

    // Header for the result count strip. Shows the total filtered
    // count so the user knows how many projects matched their query.
    var resultsLabel = data.searchQuery
      ? data.totalFiltered + " match for \"" + data.searchQuery + "\""
      : data.totalFiltered + " project" + (data.totalFiltered === 1 ? "" : "s");

    return h("div", null,
      h("div", { className: "flex items-center gap-3 mb-4 flex-wrap" },
        h("div", { className: "flex-1 min-w-[200px]" },
          h(SearchInput, { value: data.searchQuery, onChange: handleSearchChange })
        ),
        h("div", { className: "relative" },
          h("select", {
            className: "text-sm border border-border rounded-md px-3 py-1.5 bg-background text-foreground appearance-none pr-8 cursor-pointer hover:bg-accent transition-colors",
            value: data.sortBy,
            onChange: function(e) { handleSortChange(e.target.value); }
          },
            h("option", { value: "lastCommit" }, "Last commit"),
            h("option", { value: "name" }, "Name"),
            h("option", { value: "branchCount" }, "Branch count")
          ),
          h("span", { className: "absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none text-xs" }, "\u25BC")
        ),
        h(RefreshButton, { onRefresh: loadProjects }),
        h(PullAllButton, {
          onPullAll: function() { setTimeout(loadProjects, 1000); },
          onToast: showToast
        })
      ),

      // Results count + page-size selector strip.
      h("div", { className: "flex items-center justify-between text-xs text-muted-foreground mb-2 px-1" },
        h("span", null, resultsLabel),
        h("div", { className: "flex items-center gap-1" },
          h("span", null, "Per page:"),
          h("div", { className: "inline-flex border border-border rounded-md overflow-hidden" },
            pageSizeOptions.map(function(size) {
              var isActive = data.pageSize === size;
              return h("button", {
                key: "size-" + size,
                type: "button",
                onClick: function() { setPageSize(size); },
                className:
                  "px-2.5 py-1 text-xs font-mono transition-colors " +
                  (isActive
                    ? "bg-primary text-primary-foreground"
                    : "bg-background text-foreground hover:bg-accent")
              }, size);
            })
          )
        )
      ),

      data.loading ? h(LoadingState) :
      data.error ? h(ErrorState, { message: data.error }) :
      sortedProjects.length === 0 ? h(EmptyState) :
      h("div", { className: "grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2 mt-4" },
        sortedProjects.map(function(project, index) {
          return h(ProjectCard, { key: project.id || (project.name + "-" + index), project: project, onSync: loadProjects, onToast: showToast });
        })
      ),

      // Pagination footer — only show when more than one page exists.
      data.pageCount > 1 ? h("div", { className: "flex items-center justify-center gap-1 mt-6 text-sm" },
        h("button", {
          type: "button",
          disabled: data.page === 1,
          onClick: function() { goToPage(data.page - 1); },
          className: "px-3 py-1 rounded-md border border-border bg-background text-foreground hover:bg-accent disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        }, "\u00ab Prev"),
        pageNumbers.map(function(p, idx) {
          if (p === "...") {
            return h("span", {
              key: "ellipsis-" + idx,
              className: "px-2 text-muted-foreground"
            }, "\u2026");
          }
          var isCurrent = p === data.page;
          return h("button", {
            key: "page-" + p,
            type: "button",
            onClick: function() { goToPage(p); },
            className:
              "min-w-[2rem] px-2.5 py-1 rounded-md border transition-colors " +
              (isCurrent
                ? "border-primary bg-primary text-primary-foreground font-semibold"
                : "border-border bg-background text-foreground hover:bg-accent")
          }, String(p));
        }),
        h("button", {
          type: "button",
          disabled: data.page === data.pageCount,
          onClick: function() { goToPage(data.page + 1); },
          className: "px-3 py-1 rounded-md border border-border bg-background text-foreground hover:bg-accent disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        }, "Next \u00bb")
      ) : null,

      toast ? h(Toast, { message: toast.message, type: toast.type, onClose: function() { setToast(null); } }) : null
    );
  }

  function AddFolderModal(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var useEffect = SDK.hooks.useEffect;
    var Button = SDK.components.Button;
    var Input = SDK.components.Input;

    var state = useState({ path: "", type: "scan_root", label: "", submitting: false, error: null });
    var data = state[0];
    var setData = state[1];

    var suggestionsState = useState({ items: [], open: false, loading: false, activeIndex: -1 });
    var suggestions = suggestionsState[0];
    var setSuggestions = suggestionsState[1];

    function update(patch) {
      setData(function(prev) {
        var next = {};
        for (var k in prev) next[k] = prev[k];
        for (var k2 in patch) next[k2] = patch[k2];
        return next;
      });
    }

    function handleClose() {
      if (data.submitting) return;
      if (typeof props.onClose === "function") props.onClose();
    }

    // Debounced path autocomplete: query /watch-paths/suggest 200ms after the
    // last keystroke and surface results in a dropdown under the path input.
    useEffect(function() {
      var timer = null;
      var reqId = 0;
      function onPathChange(value) {
        if (timer) clearTimeout(timer);
        var prefix = (value || "").trim();
        if (!prefix) {
          setSuggestions({ items: [], open: false, loading: false, activeIndex: -1 });
          return;
        }
        setSuggestions(function(prev) { return { items: prev.items, open: prev.open, loading: true, activeIndex: -1 }; });
        timer = setTimeout(function() {
          var myReqId = ++reqId;
          apiFetch("/watch-paths/suggest?prefix=" + encodeURIComponent(prefix))
            .then(function(response) {
              if (myReqId !== reqId) return;
              var list = response && Array.isArray(response.suggestions)
                ? response.suggestions
                : (Array.isArray(response) ? response : []);
              setSuggestions({ items: list, open: list.length > 0, loading: false, activeIndex: -1 });
            })
            .catch(function() {
              if (myReqId !== reqId) return;
              setSuggestions({ items: [], open: false, loading: false, activeIndex: -1 });
            });
        }, 200);
      }
      onPathChange(data.path);
      return function() {
        if (timer) clearTimeout(timer);
        reqId++;
      };
    }, [data.path]);

    function selectSuggestion(value) {
      update({ path: value, error: null });
      setSuggestions({ items: [], open: false, loading: false, activeIndex: -1 });
    }

    function handlePathKeyDown(e) {
      if (!suggestions.open || suggestions.items.length === 0) {
        if (e.key === "Escape") {
          setSuggestions({ items: [], open: false, loading: false, activeIndex: -1 });
        }
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        var nextDown = (suggestions.activeIndex + 1) % suggestions.items.length;
        setSuggestions(function(prev) { return { items: prev.items, open: true, loading: false, activeIndex: nextDown }; });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        var nextUp = suggestions.activeIndex <= 0
          ? suggestions.items.length - 1
          : suggestions.activeIndex - 1;
        setSuggestions(function(prev) { return { items: prev.items, open: true, loading: false, activeIndex: nextUp }; });
      } else if (e.key === "Enter") {
        if (suggestions.activeIndex >= 0 && suggestions.activeIndex < suggestions.items.length) {
          e.preventDefault();
          selectSuggestion(suggestions.items[suggestions.activeIndex]);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        setSuggestions({ items: [], open: false, loading: false, activeIndex: -1 });
      }
    }

    function handleSubmit(e) {
      e.preventDefault();
      if (data.submitting) return;
      var path = (data.path || "").trim();
      if (!path) {
        update({ error: "Path is required." });
        return;
      }
      update({ submitting: true, error: null });
      var body = { path: path, type: data.type };
      if (data.label && data.label.trim()) body.label = data.label.trim();
      apiFetch("/watch-paths", { method: "POST", body: JSON.stringify(body) })
        .then(function(created) {
          update({ path: "", type: "scan_root", label: "", submitting: false, error: null });
          if (typeof props.onAdded === "function") props.onAdded(created);
        })
        .catch(function(err) {
          var msg = (err && err.message) ? err.message : "Failed to add folder";
          update({ submitting: false, error: msg });
          if (SDK && typeof SDK.dialogs === "object" && typeof SDK.dialogs.alert === "function") {
            SDK.dialogs.alert({ title: "Add folder failed", description: msg });
          }
        });
    }

    return h("div", {
      className: "fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4",
      onClick: handleClose
    },
      h("div", {
        className: "bg-background border border-border rounded-md shadow-lg w-full max-w-md p-5",
        onClick: function(e) { e.stopPropagation(); }
      },
        h("div", { className: "flex items-center justify-between mb-4" },
          h("h2", { className: "text-lg font-semibold" }, "Add watched folder"),
          h("button", {
            type: "button",
            className: "text-muted-foreground hover:text-foreground text-xl leading-none",
            onClick: handleClose,
            "aria-label": "Close"
          }, "\u2715")
        ),
        h("form", { onSubmit: handleSubmit, className: "space-y-3" },
          h("div", null,
            h("label", { className: "block text-sm font-medium mb-1" }, "Path"),
            h("div", { className: "relative" },
              h(Input, {
                type: "text",
                placeholder: "/absolute/path/to/folder",
                value: data.path,
                onChange: function(e) { update({ path: e.target.value, error: null }); },
                onKeyDown: handlePathKeyDown,
                onBlur: function() {
                  // Delay so a click on a suggestion still registers before the dropdown closes.
                  setTimeout(function() {
                    setSuggestions(function(prev) { return { items: prev.items, open: false, loading: prev.loading, activeIndex: -1 }; });
                  }, 150);
                },
                onFocus: function() {
                  if (suggestions.items.length > 0) {
                    setSuggestions(function(prev) { return { items: prev.items, open: true, loading: prev.loading, activeIndex: prev.activeIndex }; });
                  }
                },
                autoFocus: true,
                disabled: data.submitting,
                "aria-autocomplete": "list",
                "aria-expanded": suggestions.open,
                className: "w-full"
              }),
              suggestions.open && suggestions.items.length > 0 ? h("ul", {
                key: "suggest",
                className: "absolute left-0 right-0 top-full mt-1 max-h-48 overflow-y-auto z-10 bg-popover border border-border rounded-md shadow-md py-1 text-sm"
              },
                suggestions.items.map(function(item, idx) {
                  return h("li", {
                    key: idx,
                    role: "option",
                    "aria-selected": idx === suggestions.activeIndex,
                    onMouseDown: function(e) { e.preventDefault(); selectSuggestion(item); },
                    onMouseEnter: function() {
                      setSuggestions(function(prev) { return { items: prev.items, open: true, loading: prev.loading, activeIndex: idx }; });
                    },
                    className: "px-3 py-1.5 cursor-pointer truncate " + (idx === suggestions.activeIndex ? "bg-accent text-accent-foreground" : "hover:bg-accent/60")
                  }, item);
                })
              ) : null
            )
          ),
          h("div", null,
            h("label", { className: "block text-sm font-medium mb-1" }, "Type"),
            h("div", { className: "flex gap-4 mt-1" },
              h("label", { className: "flex items-center gap-2 text-sm cursor-pointer" },
                h("input", {
                  type: "radio",
                  name: "watch-path-type",
                  value: "scan_root",
                  checked: data.type === "scan_root",
                  onChange: function() { update({ type: "scan_root" }); },
                  disabled: data.submitting
                }),
                "Scan subfolders"
              ),
              h("label", { className: "flex items-center gap-2 text-sm cursor-pointer" },
                h("input", {
                  type: "radio",
                  name: "watch-path-type",
                  value: "single_repo",
                  checked: data.type === "single_repo",
                  onChange: function() { update({ type: "single_repo" }); },
                  disabled: data.submitting
                }),
                "Single repo"
              )
            )
          ),
          h("div", null,
            h("label", { className: "block text-sm font-medium mb-1" }, "Label (optional)"),
            h(Input, {
              type: "text",
              placeholder: "Friendly name",
              value: data.label,
              onChange: function(e) { update({ label: e.target.value }); },
              disabled: data.submitting,
              className: "w-full"
            })
          ),
          data.error ? h("div", {
            className: "text-sm text-destructive border border-destructive/30 bg-destructive/10 rounded px-3 py-2"
          }, data.error) : null,
          h("div", { className: "flex items-center justify-end gap-2 pt-2" },
            h(Button, {
              type: "button",
              variant: "outline",
              onClick: handleClose,
              disabled: data.submitting
            }, "Cancel"),
            h(Button, {
              type: "submit",
              disabled: data.submitting,
              className: data.submitting ? "opacity-50 cursor-not-allowed" : ""
            }, data.submitting ? "Adding..." : "Add folder")
          )
        )
      )
    );
  }

  function WatchedFolders(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var useEffect = SDK.hooks.useEffect;
    var Card = SDK.components.Card;
    var CardHeader = SDK.components.CardHeader;
    var CardTitle = SDK.components.CardTitle;
    var CardContent = SDK.components.CardContent;
    var Badge = SDK.components.Badge;
    var Button = SDK.components.Button;

    var state = useState({ items: [], loading: true, error: null, deletingId: null, modalOpen: false, toast: null });
    var data = state[0];
    var setData = state[1];

    var modalState = useState(false);
    var modalOpen = modalState[0];
    var setModalOpen = modalState[1];

    var dragOverState = useState(null);
    var dragOverId = dragOverState[0];
    var setDragOverId = dragOverState[1];

    var draggingState = useState(null);
    var draggingId = draggingState[0];
    var setDraggingId = draggingState[1];

    var reorderState = useState(false);
    var reordering = reorderState[0];
    var setReordering = reorderState[1];

    // Inline rename: { id: row being edited, value: current input text, saving: in-flight PATCH }
    var editingState = useState({ id: null, value: "", saving: false });
    var editing = editingState[0];
    var setEditing = editingState[1];

    function loadFolders() {
      setData(function(prev) {
        return { items: prev.items, loading: true, error: null, deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: prev.toast };
      });
      apiFetch("/watch-paths")
        .then(function(response) {
          var items = response && response.watch_paths ? response.watch_paths : (Array.isArray(response) ? response : []);
          setData(function(prev) {
            return { items: items, loading: false, error: null, deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: prev.toast };
          });
        })
        .catch(function(err) {
          setData(function(prev) {
            return { items: [], loading: false, error: err.message || "Failed to load watched folders", deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: prev.toast };
          });
        });
    }

    useEffect(function() { loadFolders(); }, []);

    function showToast(message, type) {
      setData(function(prev) {
        return { items: prev.items, loading: prev.loading, error: prev.error, deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: { message: message, type: type || "info" } };
      });
      setTimeout(function() {
        setData(function(prev) {
          return { items: prev.items, loading: prev.loading, error: prev.error, deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: null };
        });
      }, 3500);
    }

    function handleAdded() {
      setModalOpen(false);
      showToast("Folder added", "success");
      loadFolders();
      if (typeof props.onChange === "function") props.onChange();
    }

    function handleDelete(item) {
      var entryPath = item && item.path ? item.path : "";
      var entryId = item && item.id;
      var perform = function() {
        if (!entryId) return;
        setData(function(prev) {
          return { items: prev.items, loading: prev.loading, error: prev.error, deletingId: entryId, modalOpen: prev.modalOpen, toast: prev.toast };
        });
        apiFetch("/watch-paths/" + encodeURIComponent(entryId), { method: "DELETE" })
          .then(function() {
            setData(function(prev) {
              return { items: prev.items, loading: prev.loading, error: prev.error, deletingId: null, modalOpen: prev.modalOpen, toast: prev.toast };
            });
            showToast("Folder removed", "success");
            loadFolders();
            if (typeof props.onChange === "function") props.onChange();
          })
          .catch(function(err) {
            setData(function(prev) {
              return { items: prev.items, loading: prev.loading, error: prev.error, deletingId: null, modalOpen: prev.modalOpen, toast: prev.toast };
            });
            var msg = (err && err.message) ? err.message : "Failed to remove folder";
            showToast(msg, "error");
            if (SDK && typeof SDK.dialogs === "object" && typeof SDK.dialogs.alert === "function") {
              SDK.dialogs.alert({ title: "Remove folder failed", description: msg });
            }
          });
      };
      if (SDK && typeof SDK.dialogs === "object" && typeof SDK.dialogs.confirm === "function") {
        SDK.dialogs.confirm({
          title: "Remove watched folder?",
          description: entryPath,
          destructive: true,
          confirmLabel: "Remove",
          cancelLabel: "Cancel",
          onConfirm: perform,
          onCancel: function() {}
        });
        return;
      }
      perform();
    }

    function handleReorder(fromId, toId) {
      if (reordering) return;
      if (!fromId || fromId === toId) return;
      var current = (data.items || []).map(function(i) { return i.id; });
      var fromIdx = current.indexOf(fromId);
      var toIdx = current.indexOf(toId);
      if (fromIdx === -1 || toIdx === -1) return;
      var newOrder = current.slice();
      newOrder.splice(fromIdx, 1);
      newOrder.splice(toIdx, 0, fromId);
      setReordering(true);
      apiFetch("/watch-paths/reorder", {
        method: "POST",
        body: JSON.stringify({ ids: newOrder })
      })
        .then(function() {
          setReordering(false);
          loadFolders();
          if (typeof props.onChange === "function") props.onChange();
        })
        .catch(function(err) {
          setReordering(false);
          var msg = (err && err.message) ? err.message : "Failed to reorder folders";
          if (SDK && typeof SDK.dialogs === "object" && typeof SDK.dialogs.alert === "function") {
            SDK.dialogs.alert({ title: "Reorder failed", description: msg });
          } else {
            showToast(msg, "error");
          }
        });
    }

    function startEditLabel(item) {
      if (!item || item.id == null) return;
      setEditing({ id: item.id, value: item.label || "", saving: false });
    }

    function cancelEditLabel() {
      setEditing({ id: null, value: "", saving: false });
    }

    function commitEditLabel(item) {
      if (!item || item.id == null) return;
      if (editing.id !== item.id) return;
      var newLabel = (editing.value || "").trim();
      // Skip PATCH on empty label — leave the row visually unchanged.
      if (!newLabel) {
        cancelEditLabel();
        return;
      }
      setEditing(function(prev) { return { id: prev.id, value: prev.value, saving: true }; });
      apiFetch("/watch-paths/" + encodeURIComponent(item.id), {
        method: "PATCH",
        body: JSON.stringify({ label: newLabel })
      })
        .then(function(updated) {
          // Reflect the new label locally without waiting for refetch.
          setData(function(prev) {
            var nextItems = (prev.items || []).map(function(it) {
              if (it.id !== item.id) return it;
              return Object.assign({}, it, { label: updated && updated.label ? updated.label : newLabel });
            });
            return { items: nextItems, loading: prev.loading, error: prev.error, deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: prev.toast };
          });
          cancelEditLabel();
          showToast("Label updated", "success");
          loadFolders();
          if (typeof props.onChange === "function") props.onChange();
        })
        .catch(function(err) {
          setEditing(function(prev) { return { id: prev.id, value: prev.value, saving: false }; });
          var msg = (err && err.message) ? err.message : "Failed to update label";
          showToast(msg, "error");
          if (SDK && typeof SDK.dialogs === "object" && typeof SDK.dialogs.alert === "function") {
            SDK.dialogs.alert({ title: "Rename failed", description: msg });
          }
        });
    }

    var items = data.items || [];
    var typeVariant = function(t) {
      return t === "scan_root" ? "default" : "secondary";
    };

    return h(Card, { className: "mb-4" },
      h(CardHeader, { className: "pb-2 pt-3 px-4" },
        h("div", { className: "flex items-center justify-between gap-2" },
          h(CardTitle, { className: "text-base font-semibold" }, "Watched folders"),
          h(Button, {
            size: "sm",
            onClick: function() { setModalOpen(true); }
          }, "+ Add folder")
        )
      ),
      h(CardContent, { className: "px-4 pb-3 pt-0" },
        data.loading ? h("div", { className: "text-sm text-muted-foreground py-2" }, "Loading watched folders...") :
        data.error ? h("div", { className: "text-sm text-destructive py-2" }, data.error) :
        items.length === 0 ? h("div", {
          className: "text-sm text-muted-foreground py-2"
        }, "No watched folders yet. Click Add folder to start.") :
        h("ul", { className: "divide-y divide-border" },
          items.map(function(item) {
            var isDragOver = dragOverId === item.id && draggingId !== null && draggingId !== item.id;
            var isDragging = draggingId === item.id;
            var rowClass = "flex items-center gap-2 py-2 transition-colors " +
              (isDragging ? "opacity-50 " : "") +
              (isDragOver ? "bg-accent/40 " : "") +
              (reordering ? "cursor-wait " : "cursor-grab active:cursor-grabbing");
            return h("li", {
              key: item.id,
              draggable: !reordering,
              className: rowClass,
              onDragStart: function(e) {
                if (!e.dataTransfer) return;
                e.dataTransfer.setData("text/plain", String(item.id));
                e.dataTransfer.effectAllowed = "move";
                setDraggingId(item.id);
              },
              onDragOver: function(e) {
                if (!e.dataTransfer) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                if (dragOverId !== item.id) setDragOverId(item.id);
              },
              onDragLeave: function() {
                if (dragOverId === item.id) setDragOverId(null);
              },
              onDragEnd: function() {
                setDraggingId(null);
                setDragOverId(null);
              },
              onDrop: function(e) {
                e.preventDefault();
                setDragOverId(null);
                setDraggingId(null);
                var fromId = parseInt(e.dataTransfer.getData("text/plain"), 10);
                handleReorder(fromId, item.id);
              }
            },
              h("span", {
                className: "text-muted-foreground select-none shrink-0 cursor-grab leading-none px-1",
                title: "Drag to reorder",
                "aria-hidden": "true"
              }, "\u22ee"),
              h("code", {
                className: "text-xs bg-muted text-foreground border border-border px-1.5 py-0.5 rounded flex-1 min-w-0 truncate",
                title: item.path
              }, item.path),
              h(Badge, {
                variant: typeVariant(item.type),
                className: "text-xs shrink-0"
              }, item.type === "scan_root" ? "scan_root" : "single_repo"),
              editing.id === item.id ? h("input", {
                key: "edit-" + item.id,
                type: "text",
                value: editing.value,
                autoFocus: true,
                disabled: editing.saving,
                placeholder: "Label",
                onChange: function(e) {
                  setEditing(function(prev) { return { id: prev.id, value: e.target.value, saving: prev.saving }; });
                },
                onKeyDown: function(e) {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitEditLabel(item);
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    cancelEditLabel();
                  }
                },
                onBlur: function() { commitEditLabel(item); },
                "aria-label": "Edit label",
                className: "text-xs bg-background border border-border rounded px-1.5 py-0.5 w-32 shrink-0 focus:outline-none focus:ring-1 focus:ring-ring"
              }) : h("span", {
                key: "label-" + item.id,
                onDoubleClick: function() { startEditLabel(item); },
                className: "text-xs text-muted-foreground shrink-0 hidden sm:inline cursor-text hover:text-foreground transition-colors " + (item.label ? "" : "italic text-muted-foreground/60"),
                title: item.label ? (item.label + " (double-click to rename)") : "Double-click to add a label"
              }, item.label || "—"),
              h(Button, {
                size: "sm",
                variant: "outline",
                disabled: data.deletingId === item.id || reordering,
                onClick: function() { handleDelete(item); },
                className: "shrink-0 " + (data.deletingId === item.id ? "opacity-50 cursor-not-allowed" : "text-destructive border-destructive/40 hover:bg-destructive/10")
              }, data.deletingId === item.id ? "Removing..." : "Delete")
            );
          })
        ),
        data.toast ? h(Toast, {
          message: data.toast.message,
          type: data.toast.type,
          onClose: function() {
            setData(function(prev) {
              return { items: prev.items, loading: prev.loading, error: prev.error, deletingId: prev.deletingId, modalOpen: prev.modalOpen, toast: null };
            });
          }
        }) : null,
        modalOpen ? h(AddFolderModal, {
          onClose: function() { setModalOpen(false); },
          onAdded: handleAdded
        }) : null
      )
    );
  }

  function WorktreeManager(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var useEffect = SDK.hooks.useEffect;
    var Card = SDK.components.Card;
    var CardHeader = SDK.components.CardHeader;
    var CardTitle = SDK.components.CardTitle;
    var CardContent = SDK.components.CardContent;
    var Badge = SDK.components.Badge;
    var Button = SDK.components.Button;

    // state.worktrees: Worktree[]   — each row from GET /worktrees
    // state.currentWorktreeId       — the active selection
    // state.loading / error / selectingId / removingId / toast — UI feedback
    var state = useState({ worktrees: [], currentWorktreeId: null, loading: true, error: null, selectingId: null, removingId: null, bulkRemoving: false, toast: null });
    var data = state[0];
    var setData = state[1];

    function showToast(message, type) {
      setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: prev.error, selectingId: prev.selectingId, removingId: prev.removingId, toast: { message: message, type: type || "info" } }; });
      setTimeout(function() {
        setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: prev.error, selectingId: prev.selectingId, removingId: prev.removingId, toast: null }; });
      }, 3500);
    }

    function alertError(title, message) {
      if (SDK && SDK.dialogs && typeof SDK.dialogs.alert === "function") {
        SDK.dialogs.alert({ title: title, description: message });
      }
    }

    function handleBulkDelete() {
      if (data.bulkRemoving || data.loading) return;
      var removable = (data.worktrees || []).filter(function(w) {
        return !w.is_current && !w.is_locked && !w.is_protected && !w.is_dirty;
      });
      var dirtyCount = (data.worktrees || []).filter(function(w) {
        return !w.is_current && !w.is_locked && !w.is_protected && w.is_dirty;
      }).length;

      // Nothing to remove at all — only the current worktree exists. The
      // header button is disabled in this state, but we double-check here so
      // a programmatic call (or a stale render) can't open a dialog.
      if (removable.length === 0 && dirtyCount === 0) {
        alertError("Nothing to remove", "Only the current worktree exists in this project.");
        return;
      }
      // Dirty-only case: no clean worktree fits the bulk path, but the user
      // might still want to force-remove them. Surface this as a dedicated
      // dialog so the wording matches the action.
      if (removable.length === 0 && dirtyCount > 0) {
        if (SDK && SDK.dialogs && typeof SDK.dialogs.confirm === "function") {
          SDK.dialogs.confirm({
            title: "Force-remove " + dirtyCount + " dirty worktree" + (dirtyCount === 1 ? "" : "s") + "?",
            description: "Every non-current worktree has uncommitted changes. Force-removing will discard those changes. This cannot be undone.",
            destructive: true,
            confirmLabel: "Force delete all",
            cancelLabel: "Cancel",
            onConfirm: function() { performBulkDelete(true); },
            onCancel: function() {}
          });
        } else if (window.confirm("Force-remove " + dirtyCount + " dirty worktree(s)? This cannot be undone.")) {
          performBulkDelete(true);
        }
        return;
      }

      // Clean worktrees exist. Show the first dialog, then optionally a
      // second one for the dirty worktrees — only if any are dirty.
      var description = "Remove " + removable.length + " clean worktree" + (removable.length === 1 ? "" : "s") + ". This cannot be undone.";
      if (SDK && SDK.dialogs && typeof SDK.dialogs.confirm === "function") {
        SDK.dialogs.confirm({
          title: "Delete " + removable.length + " worktree" + (removable.length === 1 ? "" : "s") + "?",
          description: description,
          destructive: true,
          confirmLabel: "Delete " + removable.length,
          cancelLabel: "Cancel",
          onConfirm: function() {
            if (dirtyCount > 0) {
              SDK.dialogs.confirm({
                title: "Also force-remove " + dirtyCount + " dirty worktree" + (dirtyCount === 1 ? "" : "s") + "?",
                description: "Dirty worktrees have uncommitted changes. Force-removing them will discard those changes. This cannot be undone.",
                destructive: true,
                confirmLabel: "Force delete all",
                cancelLabel: "Skip dirty",
                onConfirm: function() { performBulkDelete(true); },
                onCancel: function() { performBulkDelete(false); }
              });
            } else {
              // No dirty worktrees — skip the second dialog entirely.
              performBulkDelete(false);
            }
          },
          onCancel: function() {}
        });
      } else if (window.confirm("Delete " + removable.length + " worktree(s)? This cannot be undone.")) {
        performBulkDelete(false);
      }
    }

    function performBulkDelete(force) {
      setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: null, selectingId: prev.selectingId, removingId: prev.removingId, bulkRemoving: true, toast: prev.toast }; });
      apiFetch("/projects/" + encodeURIComponent(props.projectName) + "/worktrees", { method: "DELETE", body: JSON.stringify({ force: force }) })
        .then(function(response) {
          var removed = (response && Array.isArray(response.removed_ids)) ? response.removed_ids.length : 0;
          var skipped = (response && Array.isArray(response.skipped)) ? response.skipped.length : 0;
          var errors = (response && Array.isArray(response.errors)) ? response.errors.length : 0;
          showToast("Removed " + removed + " worktree(s)" + (skipped > 0 ? ", skipped " + skipped : "") + (errors > 0 ? ", " + errors + " error(s)" : ""), removed > 0 ? "success" : "warning");
          setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: false, error: null, selectingId: prev.selectingId, removingId: prev.removingId, bulkRemoving: false, toast: prev.toast }; });
          loadWorktrees();
          if (typeof props.onChange === "function") props.onChange();
        })
        .catch(function(err) {
          var msg = (err && err.message) ? err.message : "Bulk delete failed";
          setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: false, error: msg, selectingId: prev.selectingId, removingId: prev.removingId, bulkRemoving: false, toast: prev.toast }; });
          alertError("Bulk delete failed", msg);
        });
    }

    function loadWorktrees() {
      if (!props.projectName) {
        setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: false, error: "Select a project to view worktrees.", selectingId: prev.selectingId, removingId: prev.removingId, toast: prev.toast }; });
        return;
      }
      setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: true, error: null, selectingId: prev.selectingId, removingId: prev.removingId, toast: prev.toast }; });
      apiFetch("/projects/" + encodeURIComponent(props.projectName) + "/worktrees")
        .then(function(response) {
          var list = response && Array.isArray(response.worktrees) ? response.worktrees : [];
          var currentId = response && response.current_worktree_id ? response.current_worktree_id : null;
          setData(function(prev) { return { worktrees: list, currentWorktreeId: currentId, loading: false, error: null, selectingId: null, removingId: null, toast: prev.toast }; });
          if (typeof props.onLoaded === "function") props.onLoaded({ worktrees: list, currentWorktreeId: currentId });
        })
        .catch(function(err) {
          var msg = (err && err.message) ? err.message : "Failed to load worktrees";
          setData(function(prev) { return { worktrees: [], currentWorktreeId: null, loading: false, error: msg, selectingId: null, removingId: null, toast: prev.toast }; });
        });
    }

    useEffect(function() { loadWorktrees(); }, [props.projectName]);

    function handleSelect(worktreeId) {
      if (!worktreeId || worktreeId === data.currentWorktreeId) return;
      var target = (data.worktrees || []).find(function(w) { return w.id === worktreeId; });
      if (!target) return;
      setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: null, selectingId: worktreeId, removingId: prev.removingId, toast: prev.toast }; });
      apiFetch("/projects/" + encodeURIComponent(props.projectName) + "/worktrees/" + encodeURIComponent(worktreeId) + "/select", { method: "POST", body: JSON.stringify({}) })
        .then(function(response) {
          var list = response && Array.isArray(response.worktrees) ? response.worktrees : data.worktrees;
          var currentId = response && response.selected_id ? response.selected_id : worktreeId;
          setData(function(prev) { return { worktrees: list, currentWorktreeId: currentId, loading: false, error: null, selectingId: null, removingId: null, toast: prev.toast }; });
          showToast("Active worktree: " + (target.branch || target.path), "success");
          if (typeof props.onChange === "function") props.onChange();
        })
        .catch(function(err) {
          var msg = (err && err.message) ? err.message : "Failed to select worktree";
          setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: msg, selectingId: null, removingId: prev.removingId, toast: prev.toast }; });
          alertError("Select worktree failed", msg);
        });
    }

    function handleRemove(worktree) {
      if (!worktree || !worktree.id) return;
      if (worktree.is_current || worktree.is_locked || worktree.is_protected || worktree.is_dirty) {
        var why = worktree.is_current ? "this worktree is currently running the dashboard"
          : worktree.is_locked ? "this worktree is locked"
          : worktree.is_protected ? "this worktree is protected (" + (worktree.branch || "unknown branch") + ")"
          : "this worktree has uncommitted changes";
        alertError("Cannot remove worktree", why + ". Resolve the state first.");
        return;
      }
      var entryPath = worktree.path || worktree.branch || worktree.id;
      var perform = function() {
        setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: null, selectingId: prev.selectingId, removingId: worktree.id, toast: prev.toast }; });
        apiFetch("/projects/" + encodeURIComponent(props.projectName) + "/worktrees/" + encodeURIComponent(worktree.id), { method: "DELETE" })
          .then(function(response) {
            var list = response && Array.isArray(response.worktrees) ? response.worktrees : (data.worktrees || []).filter(function(w) { return w.id !== worktree.id; });
            var currentId = data.currentWorktreeId;
            if (response && response.removed_id && response.removed_id === currentId) {
              currentId = null;
            }
            setData(function(prev) { return { worktrees: list, currentWorktreeId: currentId, loading: false, error: null, selectingId: null, removingId: null, toast: prev.toast }; });
            showToast("Worktree removed", "success");
            if (typeof props.onChange === "function") props.onChange();
          })
          .catch(function(err) {
            var msg = (err && err.message) ? err.message : "Failed to remove worktree";
            setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: msg, selectingId: prev.selectingId, removingId: null, toast: prev.toast }; });
            alertError("Remove worktree failed", msg);
          });
      };
      if (SDK && SDK.dialogs && typeof SDK.dialogs.confirm === "function") {
        SDK.dialogs.confirm({
          title: "Remove worktree?",
          description: entryPath + (worktree.branch ? " (" + worktree.branch + ")" : ""),
          destructive: true,
          confirmLabel: "Remove",
          cancelLabel: "Cancel",
          onConfirm: perform,
          onCancel: function() {}
        });
        return;
      }
      perform();
    }

    function statusBadges(w) {
      var out = [];
      if (w.is_current) out.push(h(Badge, { key: "cur", variant: "default", className: "text-xs" }, "current"));
      if (w.is_locked) out.push(h(Badge, { key: "lck", variant: "outline", className: "text-xs border-yellow-500 text-yellow-700" }, "locked"));
      if (w.is_dirty) out.push(h(Badge, { key: "dty", variant: "outline", className: "text-xs border-orange-500 text-orange-700" }, "dirty"));
      if (w.is_protected) out.push(h(Badge, { key: "prt", variant: "outline", className: "text-xs border-blue-500 text-blue-700" }, "protected"));
      if (!w.branch) out.push(h(Badge, { key: "dh", variant: "outline", className: "text-xs border-muted-foreground text-muted-foreground" }, "detached"));
      return out;
    }

    function canRemove(w) {
      if (!w) return false;
      if (w.is_current) return false;
      if (w.is_locked) return false;
      if (w.is_protected) return false;
      if (w.is_dirty) return false;
      return true;
    }

    function removeTooltip(w) {
      if (!w) return "";
      if (w.is_current) return "Cannot remove: this worktree is currently running the dashboard";
      if (w.is_locked) return "Cannot remove: worktree is locked";
      if (w.is_protected) return "Cannot remove: protected worktree on branch " + (w.branch || "unknown");
      if (w.is_dirty) return "Cannot remove: uncommitted changes — commit or stash first";
      return "Remove worktree";
    }

    var items = data.worktrees || [];
    var totalCount = items.length;

    return h(Card, { className: "mb-4" },
      h(CardHeader, { className: "pb-2 pt-3 px-4" },
        h("div", { className: "flex items-center justify-between gap-2" },
          h(CardTitle, { className: "text-base font-semibold" }, "Worktrees"),
          h("div", { className: "flex items-center gap-2" },
            totalCount > 0 ? h("span", { className: "text-xs text-muted-foreground" }, totalCount + " total") : null,
            h(Button, {
              size: "sm",
              variant: "outline",
              disabled: data.loading || data.bulkRemoving,
              onClick: loadWorktrees,
              className: data.loading ? "opacity-50 cursor-not-allowed" : ""
            }, data.loading ? "Loading..." : "Refresh"),
            h(Button, {
              size: "sm",
              variant: "outline",
              disabled: data.loading || data.bulkRemoving || totalCount <= 1,
              onClick: handleBulkDelete,
              title: totalCount <= 1 ? "Only the current worktree exists — nothing to remove" : "Remove every non-current worktree (skips dirty unless force-confirmed)",
              className: (data.loading || data.bulkRemoving) ? "opacity-50 cursor-not-allowed" : "text-destructive border-destructive/40 hover:bg-destructive/10"
            }, data.bulkRemoving ? "Removing..." : "Delete all")
          )
        )
      ),
      h(CardContent, { className: "px-4 pb-3 pt-0" },
        data.error && totalCount === 0 ? h("div", { className: "text-sm text-destructive py-2" }, data.error) :
        data.loading && totalCount === 0 ? h("div", { className: "text-sm text-muted-foreground py-2" }, "Loading worktrees...") :
        totalCount === 0 ? h("div", { className: "text-sm text-muted-foreground py-2" }, "No worktrees found.") :
        h("ul", { className: "divide-y divide-border max-h-64 overflow-y-auto" },
          items.map(function(w) {
            var isSelected = w.id === data.currentWorktreeId;
            var isSelecting = data.selectingId === w.id;
            var isRemoving = data.removingId === w.id;
            var rowClass = "flex items-center gap-2 py-2 transition-colors " + (isSelected ? "bg-accent/30 " : "");
            return h("li", { key: w.id, className: rowClass },
              h("input", {
                type: "radio",
                name: "pm-worktree-select",
                value: w.id,
                checked: isSelected,
                disabled: isSelecting || data.loading,
                onChange: function() { handleSelect(w.id); },
                "aria-label": "Select worktree " + (w.branch || w.path),
                className: "shrink-0 cursor-pointer"
              }),
              h("div", { className: "flex-1 min-w-0 flex items-center gap-2 flex-wrap" },
                h("code", {
                  className: "text-xs bg-muted text-foreground border border-border px-1.5 py-0.5 rounded truncate min-w-0",
                  title: w.path
                }, w.path),
                h("span", { className: "flex items-center gap-1 shrink-0" }, statusBadges(w))
              ),
              h(Button, {
                size: "sm",
                variant: "outline",
                disabled: !canRemove(w) || isRemoving || data.loading,
                onClick: function() { handleRemove(w); },
                title: removeTooltip(w),
                "aria-label": "Remove worktree " + (w.branch || w.path),
                className: "shrink-0 " + ((!canRemove(w) || isRemoving) ? "opacity-50 cursor-not-allowed" : "text-destructive border-destructive/40 hover:bg-destructive/10")
              }, isRemoving ? "Removing..." : "Remove")
            );
          })
        ),
        data.toast ? h(Toast, {
          message: data.toast.message,
          type: data.toast.type,
          onClose: function() {
            setData(function(prev) { return { worktrees: prev.worktrees, currentWorktreeId: prev.currentWorktreeId, loading: prev.loading, error: prev.error, selectingId: prev.selectingId, removingId: prev.removingId, toast: null }; });
          }
        }) : null
      )
    );
  }

  function BranchPicker(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var useEffect = SDK.hooks.useEffect;
    var Card = SDK.components.Card;
    var CardHeader = SDK.components.CardHeader;
    var CardTitle = SDK.components.CardTitle;
    var CardContent = SDK.components.CardContent;
    var Badge = SDK.components.Badge;

    var state = useState({ branches: [], currentBranch: null, loading: true, error: null, busy: false, toast: null });
    var data = state[0];
    var setData = state[1];

    function showToast(message, type) {
      setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: prev.loading, error: prev.error, busy: prev.busy, toast: { message: message, type: type || "info" } }; });
      setTimeout(function() {
        setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: prev.loading, error: prev.error, busy: prev.busy, toast: null }; });
      }, 3500);
    }

    function alertError(title, message) {
      if (SDK && SDK.dialogs && typeof SDK.dialogs.alert === "function") {
        SDK.dialogs.alert({ title: title, description: message });
      }
    }

    function loadBranches() {
      if (!props.projectName) {
        setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: false, error: "Select a project to view branches.", busy: prev.busy, toast: prev.toast }; });
        return;
      }
      setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: true, error: null, busy: prev.busy, toast: prev.toast }; });
      apiFetch("/projects/" + encodeURIComponent(props.projectName) + "/worktrees/branches")
        .then(function(response) {
          var list = response && Array.isArray(response.branches) ? response.branches : [];
          var current = response && response.current_branch ? response.current_branch : null;
          setData(function(prev) { return { branches: list, currentBranch: current, loading: false, error: null, busy: false, toast: prev.toast }; });
        })
        .catch(function(err) {
          var msg = (err && err.message) ? err.message : "Failed to load branches";
          setData(function(prev) { return { branches: [], currentBranch: null, loading: false, error: msg, busy: false, toast: prev.toast }; });
        });
    }

    // Initial mount load. The parent bumps `repoTick` to force a remount
    // when the active worktree or branch changes — that re-runs this effect,
    // so a single mount-only hook covers both the first paint and the
    // worktree-driven reload.
    useEffect(function() { loadBranches(); }, [props.projectName]);

    function handleCheckout(branchName) {
      if (!branchName || branchName === data.currentBranch) return;
      var branch = (data.branches || []).find(function(b) { return b.name === branchName; });
      if (!branch || branch.is_remote) return;

      // Three-step checkout: try plain POST first. If the working tree is
      // dirty the backend returns 400 WORKTREE_DIRTY. We surface that
      // case with a confirm dialog offering Stash / Force / Cancel.
      performCheckout(branchName, /* stash */ false, /* force */ false);

      function performCheckout(targetBranch, stash, force) {
        setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: prev.loading, error: null, busy: true, toast: prev.toast }; });
        var body = { branch: targetBranch };
        if (stash) body.stash = true;
        if (force) body.force = true;
        apiFetch("/projects/" + encodeURIComponent(props.projectName) + "/worktrees/checkout", { method: "POST", body: JSON.stringify(body) })
          .then(function(response) {
            var current = response && response.branch ? response.branch : targetBranch;
            var toastMsg = "Checked out: " + current;
            if (response && response.stashed) toastMsg += " (changes stashed)";
            setData(function(prev) { return { branches: prev.branches, currentBranch: current, loading: false, error: null, busy: false, toast: prev.toast }; });
            showToast(toastMsg, "success");
            if (typeof props.onChanged === "function") props.onChanged();
          })
          .catch(function(err) {
            var msg = (err && err.message) ? err.message : "Checkout failed";
            // WORKTREE_DIRTY → offer the user Stash/Force/Cancel.
            if (/uncommitted changes/i.test(msg) && SDK && SDK.dialogs && typeof SDK.dialogs.confirm === "function") {
              setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: false, error: null, busy: false, toast: prev.toast }; });
              SDK.dialogs.confirm({
                title: "Uncommitted changes prevent checkout",
                description: "Branch '" + targetBranch + "' requires a clean working tree. Stash the changes (you can `git stash pop` later), force the checkout (may lose uncommitted work), or cancel.",
                destructive: true,
                confirmLabel: "Stash & checkout",
                cancelLabel: "Force checkout",
                onConfirm: function() { performCheckout(targetBranch, /* stash */ true, /* force */ false); },
                onCancel: function() { performCheckout(targetBranch, /* stash */ false, /* force */ true); }
              });
              return;
            }
            setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: prev.loading, error: msg, busy: false, toast: prev.toast }; });
            alertError("Checkout failed", msg);
          });
      }
    }

    function isOptionDisabled(b) {
      if (!b) return true;
      if (b.is_current) return true;
      if (b.is_remote) return true;
      return false;
    }

    var branches = data.branches || [];
    var hasBranches = branches.length > 0;

    return h(Card, { className: "mb-4" },
      h(CardHeader, { className: "pb-2 pt-3 px-4" },
        h("div", { className: "flex items-center justify-between gap-2" },
          h(CardTitle, { className: "text-base font-semibold" }, "Branch"),
          h("button", {
            type: "button",
            onClick: loadBranches,
            disabled: data.loading,
            "aria-label": "Refresh branches",
            className: "text-xs text-muted-foreground hover:text-foreground transition-colors " + (data.loading ? "opacity-50 cursor-not-allowed" : "")
          }, data.loading ? "Loading..." : "Refresh")
        )
      ),
      h(CardContent, { className: "px-4 pb-3 pt-0" },
        data.error && !hasBranches ? h("div", { className: "text-sm text-destructive py-2" }, data.error) :
        !hasBranches && data.loading ? h("div", { className: "text-sm text-muted-foreground py-2" }, "Loading branches...") :
        !hasBranches ? h("div", { className: "text-sm text-muted-foreground py-2" }, "No branches available.") :
        h("div", { className: "space-y-2" },
          h("label", { htmlFor: "pm-branch-select", className: "block text-sm font-medium" }, "Local branch"),
          h("div", { className: "flex items-center gap-2" },
            h("select", {
              id: "pm-branch-select",
              value: data.currentBranch || "",
              disabled: data.busy || data.loading,
              onChange: function(e) { handleCheckout(e.target.value); },
              "aria-busy": data.busy ? "true" : "false",
              className: "flex-1 text-sm border border-border rounded-md px-3 py-1.5 bg-background text-foreground disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer hover:bg-accent transition-colors"
            },
              branches.map(function(b, i) {
                var label = b.name + (b.is_current ? " (current)" : "") + (b.is_remote ? " (remote)" : "");
                return h("option", {
                  key: "b-" + i + "-" + b.name,
                  value: b.name,
                  disabled: isOptionDisabled(b)
                }, label);
              })
            ),
            data.busy ? h("span", { className: "text-xs text-muted-foreground shrink-0" }, "Working...") : null,
            data.currentBranch ? h(Badge, { variant: "outline", className: "text-xs shrink-0" }, data.currentBranch) : null
          ),
          data.error && hasBranches ? h("p", { className: "text-xs text-destructive", role: "alert" }, data.error) : null
        ),
        data.toast ? h(Toast, {
          message: data.toast.message,
          type: data.toast.type,
          onClose: function() {
            setData(function(prev) { return { branches: prev.branches, currentBranch: prev.currentBranch, loading: prev.loading, error: prev.error, busy: prev.busy, toast: null }; });
          }
        }) : null
      )
    );
  }


  function WorktreeBranchPanel(props) {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;
    var Card = SDK.components.Card;
    var CardHeader = SDK.components.CardHeader;
    var CardTitle = SDK.components.CardTitle;
    var CardContent = SDK.components.CardContent;
    var Button = SDK.components.Button;

    var state = useState({ open: false, repoTick: 0 });
    var open = state[0].open;
    var repoTick = state[0].repoTick;
    var setData = state[1];

    function toggle() { setData({ open: !open, repoTick: repoTick }); }
    function bumpRepo() { setData({ open: open, repoTick: repoTick + 1 }); }

    if (!props.projectName) return null;

    return h(Card, { className: "mt-2 border-border/60" },
      h(CardHeader, { className: "px-3 pt-2 pb-1 cursor-pointer select-none", onClick: toggle },
        h("div", { className: "flex items-center justify-between gap-2" },
          h(CardTitle, { className: "text-xs font-semibold" }, "Worktrees & branches"),
          h("span", { className: "text-xs text-muted-foreground" }, open ? "▾" : "▸")
        )
      ),
      open ? h(CardContent, { className: "px-3 pb-3 pt-0" },
        h("div", { className: "grid grid-cols-1 gap-2" },
          h(WorktreeManager, {
            key: "wm-" + props.projectName + "-" + repoTick,
            projectName: props.projectName,
            onChange: bumpRepo
          }),
          h(BranchPicker, {
            key: "bp-" + props.projectName + "-" + repoTick,
            projectName: props.projectName,
            onChanged: bumpRepo
          })
        )
      ) : null
    );
  }

  function ProjectManagerPage() {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var React = SDK.React;
    var h = React.createElement;
    var useState = SDK.hooks.useState;

    var refreshState = useState(0);
    var refreshTick = refreshState[0];
    var setRefreshTick = refreshState[1];

    // The worktree + branch UI is now rendered per-project inside ProjectCard
    // (so the user can edit worktrees for every project on the page, not just
    // one). The two top-level blocks were removed in this revision — see
    // ProjectCard's WorktreeManager/BranchPicker invocation block.

    return h("div", { className: "p-4 max-w-7xl mx-auto" },
      h("h1", { className: "text-2xl font-bold mb-4" }, "Project Manager"),
      h(WatchedFolders, {
        key: "watched-folders-" + refreshTick,
        onChange: function() { setRefreshTick(refreshTick + 1); }
      }),
      h(ProjectList, { key: "project-list-" + refreshTick })
    );
  }

  function init() {
    var SDK = window.__HERMES_PLUGIN_SDK__;
    var REG = window.__HERMES_PLUGINS__;

    if (!SDK || !REG || typeof REG.register !== "function") {
      elapsed += INTERVAL;
      if (elapsed >= MAX_WAIT) {
        console.error("[project-manager] SDK timeout — __HERMES_PLUGIN_SDK__ or __HERMES_PLUGINS__.register not available");
        return;
      }
      setTimeout(init, INTERVAL);
      return;
    }

    REG.register("project-manager", ProjectManagerPage);
  }

  init();
})();
