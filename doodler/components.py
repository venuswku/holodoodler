# Standard library imports
import datetime
import json
import logging
import os
import pathlib
import time

from typing import List, Optional

# External dependencies imports
import holoviews as hv
import imageio
import numpy as np
import pandas as pd
import param
import panel as pn
import tifffile
import PIL
from PIL import Image, ImageDraw
from osgeo import gdal

# from .segmentation.annotations_to_segmentations import label_to_colors
# from .segmentation.image_segmentation import segmentation

from doodler_engine.annotations_to_segmentations import segmentation, check_sanity, label_to_colors

logger = logging.getLogger(__name__)

# Load the bokeh extension for holoviews and panel
hv.extension('bokeh')

# Global holoviews parameters: no axis ticks and numbers on overlay plots
hv.opts.defaults(hv.opts.Overlay(xaxis='bare', yaxis='bare'))


class Toggle(pn.reactive.ReactiveHTML):
    """Button of the ClassToggleGroup.
    """

    active = param.Boolean(False, doc='If the button is toggled or not')

    klass = param.String(doc='Button name')

    color = param.String(doc='Color of the border in hex format')

    _template = """
    <button id="button" style="text-decoration: {{ 'underline' if active else 'normal' }};border-color:{{ color }};border-width:4px;border-radius:5%;padding:10px;font-weight:{{ 'bold' if active else 'normal' }}" onclick="${_update}">
        {{ klass }}
    </button>"""

    _scripts = {
        'active': """
        if (data.active) {
            button.style.fontWeight = "bold"
            button.style.textDecoration = "underline"
        } else {
            button.style.fontWeight = "normal"
            button.style.textDecoration = null
        }
        """
    }

    def _update(self, event):
        # One way update, a toggle can be only deactivated by setting .active to False programmatically.
        if not self.active:
            self.active = True


class ClassToggleGroup(pn.viewable.Viewer):
    """Component that allows to toggle a class by clicking on its colorized button.
    """

    active = param.String(doc='The active/selected class')

    class_color_mapping = param.Dict(doc='class:color mapping')

    def __init__(self, **params):
        super().__init__(**params)

        widgets = {}
        for i, (klass, color) in enumerate(self.class_color_mapping.items()):
            widget = Toggle(klass=klass, color=color)
            if i == 0:
                widget.active = True
            widget.param.watch(self._update_active, 'active')
            widgets[klass] = widget

        klass0 = next(iter(self.class_color_mapping))
        self.active = klass0
        self._widgets = widgets

    def _update_active(self, event):
        self._prev_active = self.active
        self._widgets[self._prev_active].active = False
        self.active = event.obj.klass

    def __panel__(self):
        # Add bottom margin to avoid the flexbox to overlap with a bottom widget.
        return pn.FlexBox(*self._widgets.values(), margin=(0, 0, 15, 0))


class DoodleDrawer(pn.viewable.Viewer):
    """Drawing component to draw lines with different class/color and width.

    Its `doodles` property allows to obtain the lines drawn as a list of pandas dataframes.
    """

    # Required input

    class_color_mapping = param.Dict(precedence=-1, doc='class:color mapping')

    # Optional input

    class_toggle_group_type = param.ClassSelector(
        class_=ClassToggleGroup, is_instance=False, doc='Optional toggle.'
    )

    # UI elements

    line_width = param.Integer(default=2, bounds=(1, 10), doc='Line width slider')

    clear_all = param.Event(label='Clear doodles', doc='Button to clear all the doodles')

    # Internal parameter

    class_toggle_group = param.Parameter(precedence=-1, doc='Instance of a ClassToggleGroup')

    label_class = param.Selector(precedence=-1, doc='Curent class')

    line_color = param.Selector(precedence=-1, doc='Current line color')

    def __init__(self, class_color_mapping, **params):
        self._accumulated_lines = []  # List of dataframes

        super().__init__(class_color_mapping=class_color_mapping, **params)

        classes = list(self.class_color_mapping)
        self.param.label_class.objects = classes
        self.param.label_class.default = self.label_class = classes[0]
        colors = list(self.class_color_mapping.values())
        self.param.line_color.objects = colors
        self.param.line_color.default = self.line_color = colors[0]

        if 'class_toggle_group_type' in params:
            self.class_toggle_group = self.class_toggle_group_type(class_color_mapping=self.class_color_mapping)

            def link(event):
                self.label_class = event.new

            self.class_toggle_group.param.watch(link, 'active')

        # Pipe used to initialize the draw plot and clear it in ._accumulate_drawn_lines()
        self._draw_pipe = hv.streams.Pipe(data=[])
        # The DynamicMap reacts to the parameters change to draw lines with the desired style.
        self._draw = hv.DynamicMap(self._clear_draw_cb, streams=[self._draw_pipe]).apply.opts(
            color=self.param.line_color, line_width=self.param.line_width
        ).opts(active_tools=['freehand_draw'])
        # Create a FreeHandDraw linked stream and attach it to the DynamicMap/
        # The DynamicMap plot is going to serve as a support for the draw tool,
        # and the data is going to be saved in the stream (see .element or .data).
        self._draw_stream = hv.streams.FreehandDraw(source=self._draw)

        # This Pipe is going to send lines accumulated from previous drawing 'sessions',
        # a session including all the lines drawn between a parameter change (line_width, class, ...).
        self._drawn_pipe = hv.streams.Pipe()
        self._drawn = hv.DynamicMap(self._drawn_cb, streams=[self._drawn_pipe]).apply.opts(
            color='line_color', line_width='line_width'
        )

        # Set the ._accumulate_drawn_lines() callback on parameter changes to gather
        # the lines previously drawn.
        self.param.watch(self._accumulate_drawn_lines, ['line_color', 'line_width'])

        # Store the previous label class, this is used in ._accumulate_drawn_lines
        self._prev_label_class = self.label_class

    @param.depends('label_class', watch=True)
    def _update_color(self):
        self.line_color = self.class_color_mapping[self.label_class]

    def _clear_draw_cb(self, data: List):
        """Clear the lines drawn in a session.
        """
        # data is always []
        return hv.Contours(data)

    def _drawn_cb(self, data: Optional[List[pd.DataFrame]]):
        """Plot all the lines previously drawn.
        """
        return hv.Contours(data, kdims=['x', 'y'], vdims=['line_color', 'line_width'])

    def _accumulate_drawn_lines(self, event: Optional[param.parameterized.Event] = None):
        """Accumulate the drawn lines, clear the drawing plot and plot all
        the drawn lines.
        """
        # dframe() on a stream element that has multiple lines return a dataframe
        # with an empty line (filled with np.nan) separating the lines. To avoid
        # having to deal with that, .split() is used to obtain a dataframe per line.
        lines = [element.dframe() for element in self._draw_stream.element.split()]
        lines = [df_line for df_line in lines if not df_line.empty]
        if not lines:
            return
        # Add to each dataframe/line its properties and its label class
        for df_line in lines:
            for ppt in ['line_width', 'line_color']:
                if event:
                    df_line[ppt] = event.old if event.name == ppt else getattr(self, ppt)
                else:
                    # New event means that we want the current properties.
                    df_line[ppt] = getattr(self, ppt)
            df_line['label_class'] = self._prev_label_class
        self._accumulated_lines.extend(lines)
        # Clear the plot from the lines just drawn
        self._draw_pipe.event(data=[])
        # Clear the draw stream
        self._draw_stream.event(data={})
        # Plot all the lines drawn at this stage by sending them through this Pipe
        self._drawn_pipe.event(data=self._accumulated_lines)

        self._prev_label_class = self.label_class

    @param.depends('clear_all', watch=True)
    def _update_clear(self):
        self.clear()

    def clear(self):
        self._accumulated_lines = []
        self._draw_pipe.event(data=[])
        self._drawn_pipe.event(data=[])
        self._draw_stream.event(data={})

    def within(self, bbox):
        """
        Return True if the doodles are all within the given bounding box.
        """
        l, b, r, t = bbox
        for d in self.doodles:
            if d['x'].min() < l or d['x'].max() > r or d['y'].min() < b or d['y'].max() > t:
                return False
        return True

    @property
    def classes(self):
        return list(self.class_color_mapping.keys())

    @property
    def colormap(self):
        return list(self.class_color_mapping.values())

    @property
    def plot(self):
        return self._drawn * self._draw

    @property
    def doodles(self) -> List[pd.DataFrame]:
        if self._draw_stream.data:
            self._accumulate_drawn_lines()
        return self._accumulated_lines


def doodles_as_array(
    doodles: List[pd.DataFrame],
    img_width: int,
    img_height: int,
    colormap: List[str],
) -> np.ndarray:
    """Turn doodle lines into Numpy arrays. The line width is taken into account.
    """
    pimg = PIL.Image.new('L', (img_width, img_height), 0)
    drawing = ImageDraw.Draw(pimg)
    for doodle in doodles:
        # Project each line from the bokeh coordinate system to the one required to create them with PIL.
        # List of vertices (x, y)
        vertices = list(doodle[['x', 'y']].itertuples(index=False, name=None))
        # There's a unique width per line
        line_width = doodle.loc[0, 'line_width']
        # Index of the colormap + 1
        line_color = doodle.loc[0, 'line_color']
        fill_value = colormap.index(line_color) + 1
        drawing.line(
            vertices,
            width=line_width,
            fill=fill_value,
            joint='curve'
        )
    return np.array(pimg)


class InputImage(param.Parameterized):
    """Component to select an image among a list and visualize it"""

    # UI elements

    location = param.Selector(label='Input image (.JPEG or .TIFF)', doc='Current image path')

    # Internal

    img_bounds = param.NumericTuple(None, length=4, doc='Bounding box in pixels.')

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.pane.HoloViews(sizing_mode='scale_height', min_height=300)
        self._load_image()

    @classmethod
    def from_folder(cls, imgs_folder, **params):
        """Return a list of JPG, JPEG, TIF, or TIFF images in a folder (not recursively).
        """
        imfiles = [
            p
            for p in pathlib.Path(imgs_folder).iterdir()
            if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.tif', '.tiff')
        ]
        imfiles = sorted(imfiles)
        input_image = cls(**params)
        input_image.param.location.objects = imfiles
        input_image.location = imfiles[0]
        return input_image

    @staticmethod
    def read_from_fs(path) -> np.ndarray:
        """Read tif or jpeg as an nd np array.
        """
        _, ext = os.path.splitext(path)

        if ext.lower() in ('.jpg', '.jpeg'):
            img = Image.open(path)
            if img.mode == 'CMYK':
                img = img.convert('RGB')
        elif ext.lower() in ('.tif', '.tiff'):
            img = tifffile.imread(str(path))
            # dataset = gdal.Open(str(path))
            # num_bands = dataset.RasterCount
            # if num_bands >= 3:
            #     red = dataset.GetRasterBand(1).ReadAsArray()
            #     green = dataset.GetRasterBand(2).ReadAsArray()
            #     blue = dataset.GetRasterBand(3).ReadAsArray()
            #     arr = np.dstack([red, green, blue])
        arr = np.array(img)

        # array is (nrows, ncols, nbands)
        return arr

    @param.depends('location', watch=True)
    def _load_image(self):
        if not self.location:
            self._plot = self._pane.object = hv.RGB(data=[])
            return
        self.array = array = self.read_from_fs(self.location)
        
        # this is where we want to split the image array used for doodling
        # and the n-band array for segmentation
        if np.ndim(array) <=2:
            array = np.dstack((array,array,array))
        
        h, w, nbands = array.shape
        if nbands > 3:
            img = array[:, :, 0:3].copy()
        else:
            img = array.copy()

        # Make sure image array is within the range
        # [0, 255] for integers or [0, 1] for floats.
        if np.issubdtype(img.dtype, np.integer) and not (np.all(img >= 0) and np.all(img <= 255)):
            img = (img / np.amax(img) * 255).astype(np.uint8)
        elif np.issubdtype(img.dtype, np.floating) and not (np.all(img >= 0) and np.all(img <= 1)):
            img = img / np.amax(img)
            img[img == float("-inf")] = 0

        # Preserve the aspect ratio
        self.img_bounds = (0, 0, w, h)
        self._plot = self._pane.object = hv.RGB(
            img, bounds=self.img_bounds
        ).opts(aspect=(w / h))

    def remove_img(self):
        """Remove the current image and get the next one if available.
        """
        locations = self.param.location.objects.copy()
        idx = locations.index(self.location)
        locations.pop(idx)
        self.param.location.objects = locations
        if locations:
            try:
                self.location = locations[idx]
            except IndexError:
                self.location = locations[0]
        else:
            self.location = None

    @property
    def plot(self):
        """
        RGB HoloViews element of the selected image.
        """
        return self._plot

    @property
    def pane(self):
        """
        Panel HoloViews pane.
        """
        return self._pane


class ComputationSettings(pn.viewable.Viewer):
    """All the parameters required by the algorithms perfoming the segmentation.

    Parameters defined with a precedence greater than _BASIC are automatically
    displayed. Parameters with negative precedence are never displayed. The
    remaining parameters are displayed by clicking on the Advanced check box.
    """

    # Precedence thresholds

    _ADVANCED = 0
    _BASIC = 10

    # TODO: UI to distinguish between post-processing and classifier settings

    advanced = param.Boolean(default=False)

    # Post-processing settings

    crf_theta = param.Number(default=1, bounds=(1, 100), step=1, label="Blur factor", precedence=11)

    crf_mu = param.Number(default=1, bounds=(1, 100), step=1, label="Model independence factor", precedence=1)

    crf_downsample_factor = param.Integer(default=2, bounds=(1, 6), label="CRF downsample factor", precedence=1)

    ## no need for this paramater with doodler-engine
    # gt_prob = param.Number(default=0.9, bounds=(0.5, 0.99), step=0.1, label="Probability of doodle", precedence=1)

    # Classifier settings

    rf_downsample_value = param.Integer(default=1, bounds=(1, 20), step=1, label="Classifier downsample factor", precedence=1)

    n_sigmas = param.Integer(default=2, bounds=(2, 6), label="Number of scales", precedence=11)

    # Fixed parameters (hard-coded in Dash doodler)

    multichannel = param.Boolean(True, constant=True, precedence=-1)

    intensity = param.Boolean(True, constant=True, precedence=-1)

    edges = param.Boolean(True, constant=True, precedence=-1)

    texture = param.Boolean(True, constant=True, precedence=-1)

    sigma_min = param.Integer(1, constant=True, precedence=-1)

    sigma_max = param.Integer(16, constant=True, precedence=-1)

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.Param(self.param, display_threshold=self._BASIC, sizing_mode='stretch_width')

    @param.depends('advanced', watch=True)
    def _update_threshold(self):
        self._pane.display_threshold = self._ADVANCED if self.advanced else self._BASIC

    def as_dict(self):
        return {
            p: v
            for p, v in self.param.values().items()
            if p not in ('name', 'advanced')
        }

    def __panel__(self):
        return self._pane


class Info(pn.viewable.Viewer):
    """Colorized text box to display information to the user.
    """

    def __init__(self):
        super().__init__()
        self._pane = pn.pane.Alert(min_height=150, sizing_mode='stretch_both')

    def update(self, msg, msg_type='primary'):
        logger.info(msg)
        self._pane.object = msg
        self._pane.alert_type = msg_type

    def add(self, msg):
        logger.info(msg)
        self._pane.object += f'<br>{msg}'

    def reset(self):
        self._pane.object = ''
        self._pane.alert_type = 'primary'

    def __panel__(self):
        return self._pane


class Application(param.Parameterized):
    """Application to create a Doodler that can be served by Panel.

    The Application takes care of composing and linking the components. It
    relies on the core segmentation algorithm to compute the segmentation.

    The main components need to be instantiated before creating the Application.

        app = Application(
            settings=ComputationSettings(...),
            doodler_drawer=DoodleDrawer(...),
            input_image=InputImage(...),
            info=Info(...),
        )
        app.servable()
    """

    # Main components

    settings = param.ClassSelector(class_=ComputationSettings, is_instance=True)

    doodle_drawer = param.ClassSelector(class_=DoodleDrawer, is_instance=True)

    input_image = param.ClassSelector(class_=InputImage, is_instance=True)

    info = param.ClassSelector(class_=Info, is_instance=True)

    # Segmentation UI

    compute_segmentation = param.Event(label='Compute segmentation')

    clear_segmentation = param.Event(label='Clear segmentation')

    save_segmentation = param.Event(label='Save segmentation and continue')

    # Customizable HoloViews styles (hidden from the GUI, settable in the constructor)

    canvas_width = param.Integer(default=600)

    def __init__(self, **params):
        self._img_pane = pn.pane.HoloViews(sizing_mode='scale_height')
        super().__init__(**params)

    def _init_img_pane(self):
        self._img_pane.object = (self.input_image.plot * self.doodle_drawer.plot).opts(responsive='height')

    @param.depends('input_image.location', watch=True)
    def _reset(self):
        # Selecting a new image so reset/clear the app.
        self.doodle_drawer.clear()
        self._clear_segmentation()
        self.info.reset()

    def _init_segmentation_output(self):
        self._segmentation_color = None
        self._segmentation = None
        self._mask_doodles = None

    @param.depends('clear_segmentation', watch=True, on_init=True)
    def _clear_segmentation(self):
        self._init_img_pane()
        self._init_segmentation_output()

    @param.depends('compute_segmentation', watch=True)
    def _compute_segmentation(self):
        doodles = self.doodle_drawer.doodles
        if not doodles:
            self.info.update('Draw doodles before trying to run the algorithm.', 'danger')
            return
        # if not self.doodle_drawer.within(self.input_image.img_bounds):
        #     self.info.update('At least a doodle was found to be drawn outside of the image bounds.', 'danger')
        #     return
        if not self.input_image.location:
            self.info.update('Input image not loaded.', 'danger')
            return

        with pn.param.set_values(self._img_pane, loading=True):
            start_time = time.time()
            self.info.update('Start...')

            self.info.add('Projecting/Converting doodles into a mask...')
            _, _, img_width, img_height = self.input_image.img_bounds
            self._mask_doodles = doodles_as_array(
                doodles,
                img_width=img_width,
                img_height=img_height,
                colormap=self.doodle_drawer.colormap,
            )

            # Long computation...
            self.info.add('Core segmentation computation...')
            ## DB: this function now takes new arguments, and a different order
            ## **self.settings.as_dict() may be considered good practice, but it obscures inputs 
            ## and makes things harder for me to debug. I've rather just see the inputs spelled out
            ## in the correct order so I know for sure. 

            # self._segmentation = segmentation(
            #     img=self.input_image.array,
            #     mask=self._mask_doodles,
            #     **self.settings.as_dict(),
            # )
            self._segmentation = segmentation(
                img=self.input_image.array,
                mask=self._mask_doodles,
                crf_theta_slider_value=self.settings.as_dict()['crf_theta'],
                crf_mu_slider_value = self.settings.as_dict()['crf_mu'],
                rf_downsample_value = self.settings.as_dict()['rf_downsample_value'],
                crf_downsample_factor = self.settings.as_dict()['crf_downsample_factor'],
                n_sigmas = self.settings.as_dict()['n_sigmas'],
                multichannel = self.settings.as_dict()['multichannel'],
                intensity = self.settings.as_dict()['intensity'],
                edges = self.settings.as_dict()['edges'],
                texture = self.settings.as_dict()['texture'],
                sigma_min = self.settings.as_dict()['sigma_min'],
                sigma_max = self.settings.as_dict()['sigma_max']
            )

            ## new function of the doodler-engine
            self._segmentation = check_sanity(self._segmentation,self._mask_doodles)
            self._segmentation = np.flipud(self._segmentation)

            self.info.add('Colorizing the segmentation...')
            ## DB: New version requires "alpha" and "do_alpha"
            # self._segmentation_color = label_to_colors(
            #     self._segmentation,
            #     self.input_image.array[:, :, 0] == 0,
            #     colormap=self.doodle_drawer.colormap,
            #     color_class_offset=-1,
            # )
            if np.ndim(self.input_image.array) <= 2:
                mask=self.input_image.array[:, :] == 0
            else:
                mask=self.input_image.array[:, :, 0] == 0

            self._segmentation_color = label_to_colors(
                self._segmentation,
                mask,
                colormap=self.doodle_drawer.colormap,
                color_class_offset=-1,
                alpha=128,
                do_alpha=True
            )

            self.info.add('Rendering the results...')
            hv_segmentation_color = hv.RGB(
                self._segmentation_color, bounds=self.input_image.img_bounds
            ).opts(alpha=0.5, responsive='height')
            self._img_pane.object = self._img_pane.object * hv_segmentation_color
            duration = round(time.time() - start_time, 1)
            self.info.add(f'Process done in {duration}s.')

    @param.depends('save_segmentation', watch=True)
    def _save_segmentation(self):
        """
        TODO: Define what do save, how and where.
        """
        if self._segmentation is None:
            self.info.update('Run first a segmentation before saving.', 'danger')
            return

        self.info.update('Saving results...', 'success')
        root_res_dir = pathlib.Path('results')
        root_res_dir.mkdir(exist_ok=True)

        now = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        res_dir = root_res_dir / now
        res_dir.mkdir()

        _, ext = os.path.splitext(self.input_image.location)
        input_file_format = ext.lower()
        input_file_name = 'input' + input_file_format
        input_img_file = res_dir / input_file_name
        input_geotransform = None
        if input_file_format in ('.tif', '.tiff'):
            # Make a copy of the inputted TIFF file.
            driver = gdal.GetDriverByName('GTiff')
            driver.Register()
            input_dataset = gdal.Open(str(self.input_image.location))
            input_geotransform = input_dataset.GetGeoTransform(can_return_null=1)
            output_dataset = driver.CreateCopy(str(input_img_file), input_dataset, strict=0)
            output_dataset = None
            input_dataset = None
        else:
            imageio.imwrite(input_img_file, self.input_image.array)
        
        doodles_file = res_dir / 'doodles.png'
        imageio.imwrite(doodles_file, self._mask_doodles)
        
        col_seg_file_name = 'colorized_segmentation' + input_file_format
        if input_file_format in ('.jpg', '.jpeg'): col_seg_file_name = 'colorized_segmentation.png'
        col_seg_file = res_dir / col_seg_file_name
        if input_file_format in ('.tif', '.tiff'):
            rows, cols, num_bands = self._segmentation_color.shape
            input_dataset = gdal.Open(str(self.input_image.location))
            # Create a TIFF output file with the segmentation result and georeferencing information (if the file is a GeoTIFF).
            driver = gdal.GetDriverByName('GTiff')
            driver.Register()
            output_dataset = driver.Create(str(col_seg_file), cols, rows, num_bands, gdal.GDT_Byte, ['PHOTOMETRIC=RGB', 'ALPHA=YES'])
            if input_geotransform is not None:
                output_dataset.SetGeoTransform(input_geotransform)
                output_dataset.SetProjection(input_dataset.GetProjection())
            # Write each color and alpha channel as a raster band in the TIFF file.
            for i in range(num_bands):
                seg_band = np.asarray(self._segmentation_color[:, :, i].copy())
                output_dataset.GetRasterBand(i+1).WriteArray(seg_band)
            # Flush the cache to save its data to the new TIFF file.
            output_dataset.FlushCache()
            # Close datasets to complete writing and flushing the output dataset to the local disk.
            output_dataset = None
            input_dataset = None
        else:
            imageio.imwrite(col_seg_file, self._segmentation_color)
        
        overlay_file_name = 'overlay' + input_file_format
        if input_file_format in ('.jpg', '.jpeg'): overlay_file_name = 'overlay.png'
        overlay_file_path = res_dir / overlay_file_name
        if input_file_format in ('.tif', '.tiff'):
            if input_geotransform is not None:  # input file is a GeoTIFF
                overlay_file = gdal.Warp(str(overlay_file_path), [str(input_img_file), str(col_seg_file)], format='GTiff')
                overlay_file = None
            else:   # input file is a normal TIFF file without georeferenced coordinates
                input_img = Image.fromarray(np.uint8(self.input_image.array)).convert('RGBA')
                seg_img = Image.fromarray(np.uint8(self._segmentation_color)).convert('RGBA')
                overlay_file = Image.blend(input_img, seg_img, 0.5)
                overlay_file.save(overlay_file_path)
        else:   # input file is a JPG/JPEG
            input_img = Image.open(input_img_file).convert('RGBA')
            seg_img = Image.open(col_seg_file).convert('RGBA')
            overlay_file = Image.blend(input_img, seg_img, 0.5)
            overlay_file.save(overlay_file_path)

        content = {}
        content['time'] = now
        content['user'] = 'placeholder'
        content['settings'] = self.settings.as_dict()
        content['classes'] = self.doodle_drawer.classes
        content['colormap'] = self.doodle_drawer.colormap
        in_ = {}
        in_['image'] = str(input_img_file)
        content['input'] = in_
        out = {}
        out['doodles'] = str(doodles_file)
        out['colorized_segmentation'] = str(col_seg_file)
        out['overlay'] = str(overlay_file_path)
        content['output'] = out

        json_file = res_dir / 'info.json'
        with open(json_file, 'w') as finfo:
            json.dump(content, finfo, indent=4)
        self.info.add('Done! Onto the next one!')

    @param.depends('_save_segmentation', watch=True)
    def _remove_image(self):
        self.input_image.remove_img()

    @property
    def plot_pane(self):
        return self._img_pane
