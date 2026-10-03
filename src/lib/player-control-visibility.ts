// One activity lifecycle for transport and accessory controls.
export function attachPlayerControlVisibility(
  player: HTMLElement,
  video: HTMLVideoElement,
  strip: HTMLElement,
  onVisible: (visible: boolean) => void,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let keyboard = false;
  const held = () =>
    video.paused ||
    video.ended ||
    video.seeking ||
    strip.querySelector('[data-scrubbing="true"]') !== null ||
    strip.querySelector('[aria-expanded="true"]') !== null ||
    (strip.contains(document.activeElement) && keyboard);
  const clear = () => clearTimeout(timer);
  const reveal = () => {
    clear();
    onVisible(true);
    if (!held()) {
      timer = setTimeout(() => {
        if (!held()) onVisible(false);
      }, 3000);
    }
  };
  const pointer = (event: Event) => {
    keyboard = false;
    if (
      (event.type === "pointerdown" || event.type === "touchstart") &&
      !strip.contains(event.target as Node) &&
      strip.contains(document.activeElement)
    ) {
      (document.activeElement as HTMLElement).blur();
    }
    reveal();
  };
  const changed = (event: Event) => {
    // Touch/native pickers retain focus after dismissal. Release that hold once
    // a pointer selection is committed; keyboard users retain their focus.
    if (!keyboard && event.target instanceof HTMLSelectElement) event.target.blur();
    reveal();
  };
  const key = () => {
    keyboard = true;
    reveal();
  };
  const enter = (event: Event) => {
    void event;
    reveal();
  };
  const leave = () => {
    reveal();
  };
  // Focusout fires before activeElement changes; use relatedTarget to release
  // the hold without polling or leaving a focused menu stranded under a fade.
  const blur = (event: Event) => {
    if (!strip.contains((event as FocusEvent).relatedTarget as Node | null)) {
      clear();
      timer = setTimeout(reveal, 0);
    }
  };
  const listeners: [EventTarget, string, EventListener][] = [
    [player, "pointermove", pointer],
    [player, "pointerdown", pointer],
    [player, "touchstart", pointer],
    [player, "keydown", key],
    [player, "focusin", reveal],
    [strip, "focusout", blur],
    [strip, "change", changed],
    [strip, "click", reveal],
    [strip, "pvcontrolmenuchange", reveal],
    [strip, "pvcontrolscrubchange", reveal],
    [strip, "pointerenter", enter],
    [strip, "pointerleave", leave],
    [document, "fullscreenchange", reveal],
    [player, "pvfullscreenchange", reveal],
    ...[
      "playing",
      "pause",
      "ended",
      "seeking",
      "seeked",
      "emptied",
      "webkitbeginfullscreen",
      "webkitendfullscreen",
    ].map((event): [EventTarget, string, EventListener] => [video, event, reveal]),
  ];
  // Capture events before native/custom child handlers can stop propagation.
  listeners.forEach(([target, event, handler]) => target.addEventListener(event, handler, true));
  reveal();
  return () => {
    clear();
    listeners.forEach(([target, event, handler]) =>
      target.removeEventListener(event, handler, true),
    );
  };
}
