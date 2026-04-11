import curses


def _show_detail_modal(stdscr, candidate: dict):
    """Show a centered modal with full candidate details. ESC/q to dismiss."""
    while True:
        height, width = stdscr.getmaxyx()

        pad_x, pad_y = 4, 2
        modal_w = min(width - pad_x * 2, 80)
        modal_h = height - pad_y * 2
        start_x = (width - modal_w) // 2
        start_y = pad_y

        wrap_w = modal_w - 4

        def _wrap(text: str, indent: str = "  ") -> list[str]:
            wrapped = []
            words = text.split()
            current = indent
            for word in words:
                if len(current) + len(word) + 1 > wrap_w:
                    wrapped.append(current)
                    current = indent + word
                else:
                    current += (" " if current.strip() else "") + word
            if current.strip():
                wrapped.append(current)
            return wrapped

        lines = []
        lines.append(f"  {candidate['name']}, {candidate['age']}")
        lines.extend(_wrap(candidate['location']))
        lines.extend(_wrap(candidate['occupation']))
        lines.append("")
        lines.extend(_wrap(candidate.get('vibe', '')))
        lines.append("")
        lines.extend(_wrap(f"Why: {candidate.get('why', '')}"))
        lines.append("")
        lines.append(f"  Timezone: {candidate.get('timezone', '?')}")

        stdscr.clear()

        for y in range(start_y, start_y + modal_h):
            if y >= height:
                break
            stdscr.addstr(y, start_x, "|", curses.A_DIM)
            stdscr.addstr(y, start_x + modal_w - 1, "|", curses.A_DIM)
        top_border = "+" + "-" * (modal_w - 2) + "+"
        bot_border = "+" + "-" * (modal_w - 2) + "+"
        stdscr.addstr(start_y, start_x, top_border[:width - start_x], curses.A_DIM)
        if start_y + modal_h - 1 < height:
            stdscr.addstr(start_y + modal_h - 1, start_x, bot_border[:width - start_x], curses.A_DIM)

        title = f" {candidate['name']} "
        stdscr.addstr(start_y, start_x + (modal_w - len(title)) // 2,
                       title, curses.A_BOLD | curses.A_REVERSE)

        for i, line in enumerate(lines):
            row = start_y + 2 + i
            if row >= start_y + modal_h - 2:
                break
            text = line[:modal_w - 4]
            stdscr.addstr(row, start_x + 1, text)

        dismiss = " any key to close "
        if start_y + modal_h - 1 < height:
            stdscr.addstr(start_y + modal_h - 1,
                           start_x + (modal_w - len(dismiss)) // 2,
                           dismiss, curses.A_DIM)

        stdscr.refresh()
        stdscr.getch()
        return


def selection_ui(stdscr, candidates: list[dict],
                 held_indices: set[int]) -> tuple[set[int], str]:
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_WHITE, -1)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)

    cursor = 0
    scroll_offset = 0

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        visible_rows = height - 5

        n_held = len(held_indices)
        header = f" Friend Selection ({n_held} invited) "
        stdscr.addstr(0, 0, header, curses.A_BOLD | curses.A_REVERSE)

        col_mark = 7
        col_name = 12
        col_loc = 20
        col_vibe = max(10, width - col_mark - col_name - col_loc - 4)

        hdr_line = f" {'':6s} {'Name':<{col_name}s} {'Location':<{col_loc}s} {'Vibe'}"
        stdscr.addstr(1, 0, hdr_line[:width - 1], curses.A_DIM)
        stdscr.addstr(2, 0, "-" * min(width - 1, col_mark + col_name + col_loc + col_vibe + 4))

        if cursor < scroll_offset:
            scroll_offset = cursor
        if cursor >= scroll_offset + visible_rows:
            scroll_offset = cursor - visible_rows + 1

        for i in range(visible_rows):
            idx = scroll_offset + i
            if idx >= len(candidates):
                break
            c = candidates[idx]
            is_held = idx in held_indices
            is_cursor = idx == cursor
            row = i + 3

            marker = " INV. " if is_held else "      "
            name = c["name"][:col_name]
            loc = c["location"][:col_loc]
            vibe = c["vibe"][:col_vibe]
            line = f" {marker} {name:<{col_name}s} {loc:<{col_loc}s} {vibe}"
            line = line[:width - 1]

            if is_cursor and is_held:
                attr = curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE
            elif is_cursor:
                attr = curses.color_pair(2) | curses.A_REVERSE
            elif is_held:
                attr = curses.color_pair(1) | curses.A_BOLD
            else:
                attr = curses.color_pair(3)
            stdscr.addstr(row, 0, line, attr)

        footer_row = height - 1
        footer = " ENTER=invite  ESC/s=save+exit  x=expand  e=edit  r=re-roll  q=accept+continue "
        stdscr.addstr(footer_row, 0, footer[:width - 1], curses.color_pair(4))

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(candidates) - 1, cursor + 1)
        elif key in (ord("\n"), ord(" ")):
            if cursor in held_indices:
                held_indices.discard(cursor)
            else:
                held_indices.add(cursor)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            held_indices.discard(cursor)
        elif key == ord("x") and cursor < len(candidates):
            _show_detail_modal(stdscr, candidates[cursor])
        elif key == ord("e") and cursor < len(candidates):
            return held_indices, ("edit_candidate", cursor)
        elif key == ord("r"):
            return held_indices, "reroll"
        elif key == ord("q"):
            if n_held > 0:
                # Confirm accept
                confirm = " Accept selected friends and continue? [y/n] "
                stdscr.addstr(footer_row, 0, " " * (width - 1))
                stdscr.addstr(footer_row, 0, confirm[:width - 1],
                              curses.color_pair(4) | curses.A_BOLD)
                stdscr.refresh()
                if stdscr.getch() == ord("y"):
                    return held_indices, "accept"
            else:
                warn = " You want at least one friend right? Press ENTER to invite each friend. "
                stdscr.addstr(footer_row, 0, " " * (width - 1))
                stdscr.addstr(footer_row, 0, warn[:width - 1],
                              curses.color_pair(4) | curses.A_BOLD)
                stdscr.refresh()
                stdscr.getch()
        elif key in (ord("s"), 27):  # s or ESC
            # Confirm save+exit
            confirm = " Save selections and exit? [y/n] "
            stdscr.addstr(footer_row, 0, " " * (width - 1))
            stdscr.addstr(footer_row, 0, confirm[:width - 1],
                          curses.color_pair(4) | curses.A_BOLD)
            stdscr.refresh()
            if stdscr.getch() == ord("y"):
                return held_indices, "quit"
