# Patch

#### Fixed

- **The inbox scrolls smoothly with a fast mouse wheel inside tmux.** Wheel events are added up and drawn at most 15 times a second. Before, each event redrew the whole list, and a terminal that could not keep up (iTerm2) made tmux discard the output and redraw the window in jumps.
