import os
import io
import sys
import fcntl
import signal
import time

try:
    import urwid
except ImportError:
    urwid = None


class XiangShanSimpleTUI:
    def __init__(self, pdb, console_max_height=10, content_asm_fix_width=55, console_prefix="(xiangshan)"):
        self.asm_content = urwid.SimpleListWalker([])
        self.summary_info = urwid.SimpleListWalker([])
        self.pdb = pdb
        self.pdb.on_update_tstep = self.update_console_ouput
        self.console_input = urwid.Edit(u"%s"%console_prefix)
        self.console_input_busy = ["(wait.  )", "(wait.. )", "(wait...)"]
        self.console_input_busy_index = -1
        self.console_default_txt = "\n\n\n\n"
        self.console_outbuffer = self.console_default_txt;
        self.console_output = ANSIText(self.console_outbuffer)
        self.console_max_height = console_max_height
        self.content_asm_fix_width = content_asm_fix_width
        self.cmd_history = []
        self.cmd_history_index = 0
        self.last_key = None
        self.last_line = None
        self.complete_remain = []
        self.complete_maxshow = 100
        self.complete_tips = "\nAvailable commands:\n"
        self._pdio = io.StringIO()
        self.cpp_stderr_buffer = None
        self.cmd_is_excuting = False
        self.exit_error = None

        self.file_list = urwid.ListBox(self.asm_content)
        self.summary_pile = urwid.ListBox(self.summary_info)

        self.file_box = urwid.LineBox(
            urwid.Filler(self.file_list, valign='top', height=('relative', 100)),
            title=u"Memory Disassembly"
        )
        self.summary_box = urwid.LineBox(
            urwid.Filler(self.summary_pile, valign='top', height=("relative", 100)),
            title=u"Summary Information"
        )

        top_pane = urwid.Columns([
            (self.content_asm_fix_width, self.file_box),
            ("weight", 20, self.summary_box),
        ], dividechars=0)

        console_box = urwid.LineBox(
            urwid.Pile([
                ("flow", self.console_output),
                ('flow', self.console_input),
            ]),
            title="Console")

        self.root = urwid.Frame(
            body=urwid.Pile([
                ('weight', 1, top_pane)
            ]),
            footer=console_box,
            focus_part="footer"
        )
        self.update_asm_abs_info()
        # Note: need to update console output in the end
        self._handle_stdout_error()
        self.console_output.set_text(self._get_output("Tips: \n  Press Esc button to exit xui. \n  Ctrl+up/down/left/right to adjust the panels.\n"))

    def update_top_pane(self):
        """
        Update the layout of top_pane to reflect the new value of content_asm_fix_width.
        """
        self.root.body.contents[0] = (
            urwid.Columns([
                (self.content_asm_fix_width, self.file_box),
                ("weight", 20, self.summary_box),
            ], dividechars=0),
            ('weight', 1)
        )
        self.loop.draw_screen()

    def _redirect_stderr_on(self):
        self.original_cpp_stderr = os.dup(2)
        err_r, err_w = os.pipe()
        os.dup2(err_w, 2)
        self.cpp_stderr_buffer = os.fdopen(err_r, 'r')
        flags = fcntl.fcntl(self.cpp_stderr_buffer, fcntl.F_GETFL)
        fcntl.fcntl(self.cpp_stderr_buffer, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _handle_stdout_error(self):
        self._redirect_stderr_on()
        if getattr(self.pdb, "stdout", None):
            self.old_stdout = self.pdb.stdout
            self.pdb.stdout = self._pdio
            self.sys_stdout = sys.stdout
            sys.stdout = self._pdio
        else:
            self.old_stdout = sys.stdout
            sys.stdout = self._pdio
        if getattr(self.pdb, "stderr", None):
            self.old_stderr = self.pdb.stderr
            self.pdb.stderr = self._pdio
            self.sys_stderr = sys.stderr
            sys.stderr = self._pdio
        else:
            self.old_stderr = sys.stderr
            sys.stderr = self._pdio
    
    def _redirect_stderr_off(self):
        if self.cpp_stderr_buffer is not None:
            os.dup2(self.original_cpp_stderr, 2)
            os.close(self.original_cpp_stderr)
            self.cpp_stderr_buffer.close()
            self.cpp_stderr_buffer = None

    def _clear_stdout_error(self):
        self._redirect_stderr_off()
        if getattr(self.pdb, "stdout", None):
            self.pdb.stdout = self.old_stdout
            sys.stdout = self.sys_stdout
        else:
            sys.stdout = self.old_stdout
        if getattr(self.pdb, "stderr", None):
            self.pdb.stderr = self.old_stderr
            sys.stderr = self.sys_stderr
        else:
            sys.stderr = self.old_stderr

    def _get_pdb_out(self):
        self._pdio.flush()
        output = self._pdio.getvalue()
        if self.cpp_stderr_buffer is not None and self.cpp_stderr_buffer.readable():
            output += self.cpp_stderr_buffer.readline()
        self._pdio.truncate(0)
        self._pdio.seek(0)
        return output

    def _get_output(self, txt="", clear=False):
        if clear:
            self.console_outbuffer = txt
        if txt:
            # FIXME: why need remove duplicated '\n' ?
            buffer = (self.console_outbuffer[-1] if self.console_outbuffer else "") + txt.replace("\t", "    ")
            buffer = buffer.replace("\n\n", "\n")
            if self.console_outbuffer:
                self.console_outbuffer = self.console_outbuffer[:-1] + buffer
            else:
                self.console_outbuffer = buffer
            self.console_outbuffer = "\n".join(self.console_outbuffer.split("\n")[-self.console_max_height:])
        return self.console_outbuffer

    def handle_input(self, key):
        line = self.console_input.get_edit_text().lstrip()
        if key == 'enter':
            cmd = line
            self.console_input.set_edit_text('')
            self.process_command(cmd)
            if cmd:
                if len(self.cmd_history) == 0 or cmd != self.cmd_history[-1]:
                    self.cmd_history.append(cmd)
                self.cmd_history_index = len(self.cmd_history)
        elif key == 'esc':
            self.exit()
        elif key == 'ctrl up':
            self.console_max_height += 1
            new_text = self.console_outbuffer.split("\n")
            new_text.insert(0, "")
            self.console_outbuffer = "\n".join(new_text)
            self.console_output.set_text(self._get_output())
        elif key == 'ctrl down':
            self.console_max_height -= 1
            new_text = self.console_outbuffer.split("\n")
            new_text = new_text[1:]
            self.console_outbuffer = "\n".join(new_text)
            self.console_output.set_text(self._get_output())
        elif key == 'ctrl left':
            self.content_asm_fix_width -= 1
            self.update_top_pane()
        elif key == 'ctrl right':
            self.content_asm_fix_width += 1
            self.update_top_pane()
        elif key == "tab":
            if self.last_key == "tab" and self.last_line == line:
                end_text = ""
                cmd = self.complete_remain
                if not cmd:
                    return
                if len(cmd) > self.complete_maxshow:
                    end_text = f"\n...({len(cmd) - self.complete_maxshow} more)"
                self.console_output.set_text(self._get_output() + self.complete_tips + " ".join(cmd[:self.complete_maxshow]) + end_text)
                self.complete_remain = cmd[self.complete_maxshow:]
                return
            self.complete_remain = []
            state = 0
            cmp = []
            cmd, args, _ = self.pdb.parseline(line)
            if " " in line:
                complete_func = getattr(self.pdb, f"complete_{cmd}", None)
                if complete_func:
                    arg = args
                    if " " in args:
                        arg = args.split()[-1]
                    idbg = line.find(arg)
                    cmp = complete_func(arg, line, idbg, len(line))
            else:
                while True:
                    cmp_item = self.pdb.complete(line, state)
                    if not cmp_item:
                        break
                    state += 1
                    cmp.append(cmp_item)
            if cmp:
                prefix = os.path.commonprefix(cmp)
                full_cmd = line[:line.rfind(" ") + 1] if " " in line else ""
                if prefix:
                    full_cmd += prefix
                else:
                    full_cmd = line
                self.console_input.set_edit_text(full_cmd)
                self.console_input.set_edit_pos(len(full_cmd))
                end_text = ""
                if len(cmp) > self.complete_maxshow:
                    self.complete_remain = cmp[self.complete_maxshow:]
                    end_text = f"\n...({len(self.complete_remain)} more)"
                self.console_output.set_text(self._get_output() + self.complete_tips + " ".join(cmp[:self.complete_maxshow]) + end_text)
        elif key == "up":
            if len(self.cmd_history) > 0:
                self.cmd_history_index -= 1
                self.cmd_history_index = max(0, self.cmd_history_index)
                self.console_input.set_edit_text(self.cmd_history[self.cmd_history_index])
                self.console_input.set_edit_pos(len(self.cmd_history[self.cmd_history_index]))
        elif key == "down":
            if len(self.cmd_history) > 0:
                self.cmd_history_index += 1
                if self.cmd_history_index >= len(self.cmd_history) - 1:
                    self.cmd_history_index = len(self.cmd_history) - 1
                self.console_input.set_edit_text(self.cmd_history[self.cmd_history_index])
                self.console_input.set_edit_pos(len(self.cmd_history[self.cmd_history_index]))

        self.last_key = key
        self.last_line = line

    def update_console_ouput(self):
        self.console_output.set_text(self._get_output(self._get_pdb_out()))
        if self.console_input_busy_index >= 0:
            self.console_input_busy_index += 1
            n = self.console_input_busy_index % len(self.console_input_busy)
            self.console_input.set_caption(self.console_input_busy[n])
        self.loop.draw_screen()

    def process_command(self, cmd):
        if cmd.startswith("xload_script"):
            args = cmd.strip().split()
            if len(args) < 2:
                self.console_output.set_text(self._get_output("Usage: xload_script <script_file> [gap_time]\n"))
                return
            script_file = args[1]
            gap_time = 0
            if len(args) > 2:
                gap_time = float(args[2])
            if not os.path.exists(script_file):
                self.console_output.set_text(self._get_output(f"Error: Script file {script_file} not found.\n"))
                return
            with open(script_file, "r") as f:
                for line in f.readlines():
                    line = line.strip()
                    if line.startswith("#"):
                        continue
                    tag = "__sharp_tag_%s__" % str(time.time())
                    line = line.replace("\#", tag).split("#")[0].replace(tag, "#").strip()
                    if not line:
                        continue
                    self.process_command(line)
                    time.sleep(gap_time)

        elif cmd.startswith("xload_log"):
            args = cmd.strip().split()
            if len(args) < 2:
                self.console_output.set_text(self._get_output("Usage: xload_log <log_file> [gap_time]\n"))
                return
            log_file = args[1]
            gap_time = 0
            if len(args) > 2:
                gap_time = float(args[2])
            if not os.path.exists(log_file):
                self.console_output.set_text(self._get_output(f"Error: Log file {log_file} not found.\n"))
                return
            with open(log_file, "r") as f:
                for line in f.readlines():
                    
                    line = line[line.find(']')+1:].strip()
                    
                    if not line.startswith("------onecmd:"):
                        continue
                    
                    line = line.split("------onecmd:")[1].strip()
                    if line:
                        self.process_command(line)
                    time.sleep(gap_time)

        elif cmd == "clear":
            self.console_output.set_text(self._get_output(self.console_default_txt, clear=True))
        elif cmd in ["continue", "c", "count"]:
            self.console_output.set_text(self._get_output("continue/c/count is not supported in XUI\n"))
        else:
            self.console_output.set_text(self._get_output(cmd + "\n"))
            cap = self.console_input.caption
            self.console_input_busy_index = 0
            self.console_input.set_caption(self.console_input_busy[self.console_input_busy_index])
            self.loop.draw_screen()
            self.cmd_is_excuting = True
            original_sigint = signal.getsignal(signal.SIGINT)
            def _sigint_handler(s, f):
                self.pdb.interrupt = True
                self.console_output.set_text(self._get_output("Ctrl+C, try Exit.\n"))
            signal.signal(signal.SIGINT, _sigint_handler)
            self.pdb.onecmd(cmd)
            signal.signal(signal.SIGINT, original_sigint)
            self.cmd_is_excuting = False
            self.console_input_busy_index = -1
            self.console_input.set_caption(cap)
            self.update_asm_abs_info()
            self.update_console_ouput()

    def update_asm_abs_info(self):
        self.asm_content.clear()
        asm_size = self.get_part_size("asm")
        for l in self.pdb.get_asm_info(asm_size):
            self.asm_content.append(
                urwid.Text(l)
            )
        self.summary_info.clear()
        abs_size = self.get_part_size("abs")
        for x in self.pdb.get_abs_info(abs_size):
            self.summary_info.append(
                urwid.Text(x)
            )

    def get_part_size(self, type):
        w, h = urwid.raw_display.Screen().get_cols_rows()
        header_h = self.root.header.rows((w,)) if self.root.header else 0
        footer_h = self.root.footer.rows((w,)) if self.root.footer else 0
        h = h - header_h - footer_h
        w = w - 2
        if type == "asm":
            return self.content_asm_fix_width, h - 2
        return w - self.content_asm_fix_width, h - 2

    def exit(self, loop=None, arg=None):
        clear_success = False
        try:
            if self.exit_error is None:
                self._clear_stdout_error()
            clear_success = True
        except Exception as e:
            import traceback
            self.console_output.set_text(self._get_output("%s\n%s\n"%(str(e),
                                         traceback.format_exc())))
            self.exit_error = e
        if clear_success:
            raise urwid.ExitMainLoop()

# Color configuration (using ANSI color names)
palette = [
    ('success_green',  'light green', 'black'),
    ('norm_red',       'light red',   'black'),
    ('error_red',      'light red',   'black'),
    ('body',           'white',       'black'),
    ('divider',        'white',       'black'),
    ('border',         'white',       'black'),
    # Add ANSI color mappings
    ('black',          'black',       'black'),
    ('dark red',       'dark red',    'black'),
    ('dark green',     'dark green',  'black'),
    ('brown',          'brown',       'black'),
    ('dark blue',      'dark blue',   'black'),
    ('dark magenta',   'dark magenta','black'),
    ('dark cyan',      'dark cyan',   'black'),
    ('light gray',     'light gray',  'black'),
    ('dark gray',      'dark gray',   'black'),
    ('light red',      'light red',   'black'),
    ('light green',    'light green', 'black'),
    ('yellow',         'yellow',      'black'),
    ('light blue',     'light blue',  'black'),
    ('light magenta',  'light magenta','black'),
    ('light cyan',     'light cyan',  'black'),
    ('white',          'white',       'black'),
]


def enter_simple_xui(pdb):
    if urwid is None:
        print("urwid not found, please install urwid first.")
        return
    app = XiangShanSimpleTUI(pdb)
    loop = urwid.MainLoop(
        app.root,
        palette=palette,
        unhandled_input=app.handle_input,
        handle_mouse=False
    )
    app.loop = loop
    original_sigint = signal.getsignal(signal.SIGINT)
    def _sigint_handler(s, f):
        loop.set_alarm_in(0.0, app.exit)
    signal.signal(signal.SIGINT, _sigint_handler)
    loop.run()
    signal.signal(signal.SIGINT, original_sigint)

import re
class ANSIText(urwid.Text):
    """
    A subclass of urwid.Text that supports ANSI color codes.
    """
    ANSI_COLOR_MAP = {
        '30': 'black',
        '31': 'dark red',
        '32': 'dark green',
        '33': 'brown',
        '34': 'dark blue',
        '35': 'dark magenta',
        '36': 'dark cyan',
        '37': 'light gray',
        '90': 'dark gray',
        '91': 'light red',
        '92': 'light green',
        '93': 'yellow',
        '94': 'light blue',
        '95': 'light magenta',
        '96': 'light cyan',
        '97': 'white',
    }

    ANSI_ESCAPE_RE = re.compile(r'\x1b\[(\d+)(;\d+)*m')

    def __init__(self, text='', align='left'):
        super().__init__('', align)
        self.set_text(text)

    def set_text(self, text):
        """
        Parse the ANSI text and set it with urwid attributes.
        """
        parsed_text = self._parse_ansi(text)
        super().set_text(parsed_text)

    def _parse_ansi(self, text):
        """
        Parse ANSI escape sequences and convert them to urwid attributes.
        """
        segments = []
        current_attr = None
        pos = 0

        for match in self.ANSI_ESCAPE_RE.finditer(text):
            start, end = match.span()
            if start > pos:
                segments.append((current_attr, text[pos:start]))
            ansi_codes = match.group(0)
            current_attr = self._ansi_to_attr(ansi_codes)
            pos = end

        if pos < len(text):
            segments.append((current_attr, text[pos:]))

        return segments

    def _ansi_to_attr(self, ansi_code):
        """
        Convert ANSI escape codes to urwid attributes.
        """
        codes = ansi_code[2:-1].split(';')
        if len(codes) == 0:
            return None  # Reset attributes

        fg_code = codes[0]
        fg_color = self.ANSI_COLOR_MAP.get(fg_code, None)
        if fg_color:
            return fg_color
        return None