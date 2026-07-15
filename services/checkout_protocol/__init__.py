"""Independent UPI and Pix payment-protocol bridges."""

from .pix import generate_pix_final_link
from .upi import generate_upi_final_link

__all__ = ["generate_pix_final_link", "generate_upi_final_link"]
