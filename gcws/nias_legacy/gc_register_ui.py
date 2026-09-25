"""Small, reusable copy and keyboard affordances for the Tk register."""
import tkinter as tk


def copy_text(widget, value):
    widget.clipboard_clear()
    widget.clipboard_append(str(value))
    return 'break'


def copyable_text(parent, variable, *, height=1, bold=False):
    widget = tk.Text(parent, height=height, wrap='word', relief='flat',
                     borderwidth=0, highlightthickness=0, takefocus=True,
                     font=('Segoe UI', 12 if bold else 9, 'bold' if bold else 'normal'),
                     exportselection=False, cursor='xterm')
    def update(*_):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', variable.get())
        widget.configure(state='disabled')
    token = variable.trace_add('write', update)
    widget.bind('<Destroy>', lambda e: variable.trace_remove('write', token) if e.widget is widget else None)
    def select_all(_=None):
        widget.tag_add('sel', '1.0', 'end-1c')
        return 'break'
    widget.bind('<Control-a>', select_all)
    menu = tk.Menu(widget, tearoff=False)
    menu.add_command(label='Kopieren', command=lambda: widget.event_generate('<<Copy>>'))
    menu.add_command(label='Alles kopieren', command=lambda: copy_text(widget, variable.get()))
    def popup(event):
        widget.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
    widget.bind('<Button-3>', popup)
    update()
    return widget


def enable_tree_copy(tree, *, edit=None, extra=()):
    """Copy selected rows as TSV; context actions always target the clicked row."""
    def copy_rows(_=None):
        return copy_text(tree, '\n'.join('\t'.join(map(str, tree.item(i, 'values')))
                                        for i in tree.selection()))
    tree.bind('<Control-c>', copy_rows)
    def popup(event):
        row = tree.identify_row(event.y)
        if not row:
            return 'break'
        tree.selection_set(row)
        tree.focus(row)
        tree.focus_set()
        menu = tk.Menu(tree, tearoff=False)
        if edit:
            menu.add_command(label='Bearbeiten …', command=edit)
            menu.add_separator()
        column = tree.identify_column(event.x)
        if column and column != '#0':
            value = tree.item(row, 'values')[int(column[1:]) - 1]
            menu.add_command(label='Zelle kopieren', command=lambda: copy_text(tree, value))
        menu.add_command(label='Zeile kopieren', command=copy_rows)
        for label, command in extra:
            menu.add_command(label=label, command=command)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            menu.destroy()
        return 'break'
    tree.bind('<Button-3>', popup)
    if edit:
        def activate(_=None):
            edit()
            return 'break'
        tree.bind('<Return>', activate)
        tree.bind('<KP_Enter>', activate)
        tree.bind('<Double-1>', activate)
