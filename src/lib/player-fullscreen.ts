// Keep the PV controls in the fullscreen surface. Native video-only fullscreen
// excludes sibling controls, and container fullscreen is not available everywhere.
export function attachPlayerFullscreen(
  player: HTMLElement,
  onChange: (active: boolean) => void,
  onError: (message: string | null) => void,
) {
  const nativeAvailable =
    typeof player.requestFullscreen === "function" && document.fullscreenEnabled !== false;
  const viewportAvailable =
    typeof player.showPopover === "function" && typeof player.hidePopover === "function";
  let expanded = false;
  let disposed = false;
  const publish = () => {
    if (disposed) return;
    onChange(expanded || document.fullscreenElement === player);
    player.dispatchEvent(new Event("pvfullscreenchange"));
  };
  const collapse = () => {
    if (!expanded) return;
    expanded = false;
    player.hidePopover();
    player.removeAttribute("popover");
    delete player.dataset.playerExpanded;
    publish();
  };
  const expand = () => {
    if (disposed) return;
    player.setAttribute("popover", "manual");
    player.dataset.playerExpanded = "true";
    try {
      player.showPopover();
      expanded = true;
      publish();
    } catch (error) {
      player.removeAttribute("popover");
      delete player.dataset.playerExpanded;
      throw error;
    }
  };
  const enter = async () => {
    if (nativeAvailable) {
      try {
        await player.requestFullscreen();
        return;
      } catch {
        // A rejected API request must not leave an apparently dead button.
      }
    }
    if (viewportAvailable) expand();
    else throw new Error("Fullscreen is unavailable in this browser.");
  };
  const reportError = () => {
    if (!disposed) onError("Fullscreen could not open. Playback remains available here.");
  };
  const change = publish;
  const key = (event: KeyboardEvent) => {
    // The open menu owns Escape before fullscreen or the containing dialog.
    if (player.querySelector('[aria-expanded="true"]')) return;
    if (event.key !== "Escape" || !expanded) return;
    event.preventDefault();
    event.stopPropagation();
    collapse();
  };
  document.addEventListener("fullscreenchange", change);
  // Run before the containing dialog's document Escape listener, so leaving
  // viewport fullscreen does not also close playback.
  window.addEventListener("keydown", key, true);
  return {
    available: nativeAvailable || viewportAvailable,
    toggle: async () => {
      onError(null);
      try {
        if (expanded) collapse();
        else if (document.fullscreenElement === player) await document.exitFullscreen();
        else await enter();
      } catch {
        reportError();
      }
    },
    dispose: () => {
      disposed = true;
      document.removeEventListener("fullscreenchange", change);
      window.removeEventListener("keydown", key, true);
      collapse();
    },
  };
}
