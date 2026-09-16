import base64
from io import BytesIO

from PIL import Image


def image_bytes(format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (4, 3), (25, 80, 120)).save(buffer, format=format)
    return buffer.getvalue()


PNG = image_bytes()
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
