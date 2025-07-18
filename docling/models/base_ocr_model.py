import copy
import logging
from abc import abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Type

import numpy as np
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import TextCell
from PIL import Image, ImageDraw
from rtree import index

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import OcrOptions
from docling.datamodel.settings import settings
from docling.models.base_model import BaseModelWithOptions, BasePageModel

_log = logging.getLogger(__name__)


class BaseOcrModel(BasePageModel, BaseModelWithOptions):
    def __init__(
        self,
        *,
        enabled: bool,
        artifacts_path: Optional[Path],
        options: OcrOptions,
        accelerator_options: AcceleratorOptions,
    ):
        # Make sure any delay/error from import occurs on ocr model init and not first use
        from scipy.ndimage import binary_dilation, find_objects, label

        self.enabled = enabled
        self.options = options

    # Computes the optimum amount and coordinates of rectangles to OCR on a given page
    def get_ocr_rects(self, page: Page) -> List[BoundingBox]:
        from scipy.ndimage import binary_dilation, find_objects, label

        BITMAP_COVERAGE_TRESHOLD = 0.75
        assert page.size is not None

        def find_ocr_rects(size, bitmap_rects):
            image = Image.new(
                "1", (round(size.width), round(size.height))
            )  # '1' mode is binary

            # Draw all bitmap rects into a binary image
            draw = ImageDraw.Draw(image)
            for rect in bitmap_rects:
                x0, y0, x1, y1 = rect.as_tuple()
                x0, y0, x1, y1 = round(x0), round(y0), round(x1), round(y1)
                draw.rectangle([(x0, y0), (x1, y1)], fill=1)

            np_image = np.array(image)

            # Dilate the image by 10 pixels to merge nearby bitmap rectangles
            structure = np.ones(
                (20, 20)
            )  # Create a 20x20 structure element (10 pixels in all directions)
            np_image = binary_dilation(np_image > 0, structure=structure)

            # Find the connected components
            labeled_image, num_features = label(
                np_image > 0
            )  # Label black (0 value) regions

            # Find enclosing bounding boxes for each connected component.
            slices = find_objects(labeled_image)
            bounding_boxes = [
                BoundingBox(
                    l=slc[1].start,
                    t=slc[0].start,
                    r=slc[1].stop - 1,
                    b=slc[0].stop - 1,
                    coord_origin=CoordOrigin.TOPLEFT,
                )
                for slc in slices
            ]

            # Compute area fraction on page covered by bitmaps
            area_frac = np.sum(np_image > 0) / (size.width * size.height)

            return (area_frac, bounding_boxes)  # fraction covered  # boxes

        if page._backend is not None:
            bitmap_rects = page._backend.get_bitmap_rects()
        else:
            bitmap_rects = []
        coverage, ocr_rects = find_ocr_rects(page.size, bitmap_rects)

        # return full-page rectangle if page is dominantly covered with bitmaps
        if self.options.force_full_page_ocr or coverage > max(
            BITMAP_COVERAGE_TRESHOLD, self.options.bitmap_area_threshold
        ):
            return [
                BoundingBox(
                    l=0,
                    t=0,
                    r=page.size.width,
                    b=page.size.height,
                    coord_origin=CoordOrigin.TOPLEFT,
                )
            ]
        # return individual rectangles if the bitmap coverage is above the threshold
        elif coverage > self.options.bitmap_area_threshold:
            return ocr_rects
        else:  # overall coverage of bitmaps is too low, drop all bitmap rectangles.
            return []

    # Filters OCR cells by dropping any OCR cell that intersects with an existing programmatic cell.
    def _filter_ocr_cells(
        self, ocr_cells: List[TextCell], programmatic_cells: List[TextCell]
    ) -> List[TextCell]:
        # Create R-tree index for programmatic cells
        p = index.Property()
        p.dimension = 2
        idx = index.Index(properties=p)
        for i, cell in enumerate(programmatic_cells):
            idx.insert(i, cell.rect.to_bounding_box().as_tuple())

        def is_overlapping_with_existing_cells(ocr_cell):
            # Query the R-tree to get overlapping rectangles
            possible_matches_index = list(
                idx.intersection(ocr_cell.rect.to_bounding_box().as_tuple())
            )

            return (
                len(possible_matches_index) > 0
            )  # this is a weak criterion but it works.

        filtered_ocr_cells = [
            rect for rect in ocr_cells if not is_overlapping_with_existing_cells(rect)
        ]
        return filtered_ocr_cells

    def post_process_cells(self, ocr_cells: List[TextCell], page: Page) -> None:
        r"""
        Post-process the OCR cells and update the page object.
        Updates parsed_page.textline_cells directly since page.cells is now read-only.
        """
        # Get existing cells from the read-only property
        existing_cells = page.cells

        # Combine existing and OCR cells with overlap filtering
        final_cells = self._combine_cells(existing_cells, ocr_cells)

        assert page.parsed_page is not None

        # Update parsed_page.textline_cells directly
        page.parsed_page.textline_cells = final_cells
        page.parsed_page.has_lines = len(final_cells) > 0

    def _combine_cells(
        self, existing_cells: List[TextCell], ocr_cells: List[TextCell]
    ) -> List[TextCell]:
        """Combine existing and OCR cells with filtering and re-indexing."""
        if self.options.force_full_page_ocr:
            combined = ocr_cells
        else:
            filtered_ocr_cells = self._filter_ocr_cells(ocr_cells, existing_cells)
            combined = list(existing_cells) + filtered_ocr_cells

        # Re-index in-place
        for i, cell in enumerate(combined):
            cell.index = i

        return combined

    def draw_ocr_rects_and_cells(self, conv_res, page, ocr_rects, show: bool = False):
        image = copy.deepcopy(page.image)
        scale_x = image.width / page.size.width
        scale_y = image.height / page.size.height

        draw = ImageDraw.Draw(image, "RGBA")

        # Draw OCR rectangles as yellow filled rect
        for rect in ocr_rects:
            x0, y0, x1, y1 = rect.as_tuple()
            y0 *= scale_x
            y1 *= scale_y
            x0 *= scale_x
            x1 *= scale_x

            shade_color = (255, 255, 0, 40)  # transparent yellow
            draw.rectangle([(x0, y0), (x1, y1)], fill=shade_color, outline=None)

        # Draw OCR and programmatic cells
        for tc in page.cells:
            x0, y0, x1, y1 = tc.rect.to_bounding_box().as_tuple()
            y0 *= scale_x
            y1 *= scale_y
            x0 *= scale_x
            x1 *= scale_x

            if y1 <= y0:
                y1, y0 = y0, y1

            color = "magenta" if tc.from_ocr else "gray"

            draw.rectangle([(x0, y0), (x1, y1)], outline=color)

        if show:
            image.show()
        else:
            out_path: Path = (
                Path(settings.debug.debug_output_path)
                / f"debug_{conv_res.input.file.stem}"
            )
            out_path.mkdir(parents=True, exist_ok=True)

            out_file = out_path / f"ocr_page_{page.page_no:05}.png"
            image.save(str(out_file), format="png")

    @abstractmethod
    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:
        pass

    @classmethod
    @abstractmethod
    def get_options_type(cls) -> Type[OcrOptions]:
        pass
