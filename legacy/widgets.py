import tkinter as tk


class PlaceholderEntry(tk.Entry):
    def __init__(self, master=None, placeholder="Enter", color='#888888',
                 *args, **kwargs):
        super().__init__(master, *args, **kwargs)
        self.placeholder      = placeholder
        self.placeholder_color = color
        self.default_fg_color  = "#ffffff"
        self.bind("<FocusIn>",  self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._put_placeholder()

    def _put_placeholder(self):
        self.insert(0, self.placeholder)
        self.config(fg=self.placeholder_color)

    def _focus_in(self, event):
        if self.get() == self.placeholder:
            self.delete(0, "end")
            self.config(fg=self.default_fg_color)

    def _focus_out(self, event):
        if not self.get():
            self._put_placeholder()

    def get_value(self):
        val = self.get()
        return None if val == self.placeholder else val
