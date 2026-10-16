"""Small monochrome toolbar glyphs, rasterized without fonts or image packages."""

# Paths on an 18 x 18 canvas; separate paths are lifted pen strokes.
ICON_PATHS = {
    'bluetooth': (((9, 1), (9, 16), (14, 12), (4, 4)), ((4, 13), (14, 5), (9, 1))),
    'disconnect': (((3, 6), (3, 3), (7, 3)), ((11, 14), (14, 14), (14, 10)),
                   ((2, 15), (15, 2))),
    'usb': (((9, 15), (9, 2)), ((6, 5), (9, 2), (12, 5)),
            ((9, 11), (4, 8), (4, 5)), ((9, 13), (14, 10), (14, 7))),
    'serial': (((1, 10), (4, 10), (6, 4), (9, 14), (12, 6), (14, 10), (16, 10)),),
    'play': (((5, 2), (15, 9), (5, 16), (5, 2)),),
    'wait': (((9, 1), (14, 3), (16, 8), (14, 14), (9, 16), (3, 13), (1, 8), (4, 3), (9, 1)),
             ((9, 4), (9, 9), (12, 11))),
    'download': (((9, 1), (9, 11)), ((5, 7), (9, 11), (13, 7)),
                 ((2, 12), (2, 16), (16, 16), (16, 12))),
    'stored': (((4, 5), (7, 2), (13, 2), (16, 6), (16, 12), (12, 16), (5, 16), (2, 12)),
               ((4, 1), (4, 6), (9, 6)), ((7, 9), (11, 11), (7, 13), (7, 9))),
    'stop': (((4, 4), (13, 4), (13, 13), (4, 13), (4, 4)),),
    'race': (((3, 16), (3, 2), (15, 2), (13, 6), (15, 10), (3, 10)),
             ((7, 2), (7, 10)), ((11, 2), (11, 10)), ((3, 6), (14, 6))),
    'new': (((4, 2), (11, 2), (15, 6), (15, 16), (4, 16), (4, 2)),
            ((11, 2), (11, 6), (15, 6)), ((7, 11), (12, 11)), ((9, 8), (9, 14))),
    'export': (((3, 2), (10, 2)), ((3, 2), (3, 16), (14, 16), (14, 12)),
               ((8, 10), (16, 3)), ((11, 3), (16, 3), (16, 8))),
}


def icon_pixels(name: str, size: int = 18) -> set[tuple[int, int]]:
    """Return opaque pixels for a DPI-scaled glyph; the rest stays transparent."""
    if type(size) is not int or not 12 <= size <= 64:
        raise ValueError('Icon size must be an integer from 12 to 64')
    paths = ICON_PATHS[name]
    pixels = set()
    thickness = max(1, round(size / 18))

    def mark(x, y):
        for dx in range(thickness):
            for dy in range(thickness):
                if 0 <= x + dx < size and 0 <= y + dy < size:
                    pixels.add((x + dx, y + dy))

    def scale(point):
        return tuple(round(value * (size - 1) / 17) for value in point)

    for path in paths:
        for start, end in zip(path, path[1:]):
            x, y = scale(start)
            end_x, end_y = scale(end)
            dx, dy = abs(end_x - x), -abs(end_y - y)
            step_x, step_y = (1 if x < end_x else -1), (1 if y < end_y else -1)
            error = dx + dy
            while True:
                mark(x, y)
                if (x, y) == (end_x, end_y):
                    break
                doubled = 2 * error
                if doubled >= dy:
                    error += dy
                    x += step_x
                if doubled <= dx:
                    error += dx
                    y += step_y
    if name == 'stop':
        low, high = round(4 * (size - 1) / 17), round(13 * (size - 1) / 17)
        pixels.update((x, y) for x in range(low, high + 1) for y in range(low, high + 1))
    return pixels
