import sys
import bpy
import os
import numpy as np
from multiprocessing.shared_memory import SharedMemory

from ..preferences import StableDiffusionPreferences
from ..pil_to_image import *
from ..prompt_engineering import *
from ..absolute_path import WEIGHTS_PATH
from ..generator_process import MISSING_DEPENDENCIES_ERROR, GeneratorProcess
from ..property_groups.dream_prompt import DreamPrompt

import tempfile

generator_advance = None
last_data_block = None
timer = None

class DreamTexture(bpy.types.Operator):
    bl_idname = "shade.dream_texture"
    bl_label = "Dream Texture"
    bl_description = "Generate a texture with AI"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        global timer
        return timer is None
    
    def execute(self, context):
        history_entry = context.preferences.addons[StableDiffusionPreferences.bl_idname].preferences.history.add()
        for prop in context.scene.dream_textures_prompt.__annotations__.keys():
            if hasattr(history_entry, prop):
                setattr(history_entry, prop, getattr(context.scene.dream_textures_prompt, prop))

        def bpy_image(name, width, height, pixels):
            image = bpy.data.images.new(name, width=width, height=height)
            image.pixels[:] = pixels
            image.pack()
            return image

        node_tree = context.material.node_tree if hasattr(context, 'material') else None
        screen = context.screen
        scene = context.scene

        def image_writer(shared_memory_name, seed, width, height, upscaled=False):
            global last_data_block
            # Only use the non-upscaled texture, as upscaling is currently unsupported by the addon.
            if not upscaled:
                if last_data_block is not None:
                    bpy.data.images.remove(last_data_block)
                    last_data_block = None
                generator = GeneratorProcess.shared(create=False)
                if generator is None or generator.process.poll() or width == 0 or height == 0:
                    return # process was closed
                shared_memory = SharedMemory(shared_memory_name)
                image = bpy_image(f"{seed}", width, height, np.frombuffer(shared_memory.buf,dtype=np.float32))
                shared_memory.close()
                if node_tree is not None:
                    nodes = node_tree.nodes
                    texture_node = nodes.new("ShaderNodeTexImage")
                    texture_node.image = image
                    nodes.active = texture_node
                for area in screen.areas:
                    if area.type == 'IMAGE_EDITOR':
                        area.spaces.active.image = image
                scene.dream_textures_progress = 0
                scene.dream_textures_prompt.seed = str(seed) # update property in case seed was sourced randomly or from hash
                history_entry.seed = str(seed)
                history_entry.random_seed = False
        
        def view_step(step, width=None, height=None, shared_memory_name=None):
            scene.dream_textures_progress = step + 1
            if shared_memory_name is None:
                return # show steps disabled
            global last_data_block
            for area in screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    shared_memory = SharedMemory(shared_memory_name)
                    step_image = bpy_image(f'Step {step + 1}/{scene.dream_textures_prompt.steps}', width, height, np.frombuffer(shared_memory.buf,dtype=np.float32))
                    shared_memory.close()
                    area.spaces.active.image = step_image
                    if last_data_block is not None:
                        bpy.data.images.remove(last_data_block)
                    last_data_block = step_image
                    return # Only perform this on the first image editor found.
        dream_texture(context.scene.dream_textures_prompt, view_step, image_writer)
        return {"FINISHED"}

headless_prompt = None
headless_step_callback = None
headless_image_callback = None
headless_init_img = None
headless_args = None
def dream_texture(prompt, step_callback, image_callback, init_img=None, **kwargs):
    global headless_prompt
    headless_prompt = prompt
    global headless_step_callback
    headless_step_callback = step_callback
    global headless_image_callback
    headless_image_callback = image_callback
    global headless_init_img
    headless_init_img = init_img
    global headless_args
    headless_args = kwargs
    bpy.ops.shade.dream_texture_headless()

class HeadlessDreamTexture(bpy.types.Operator):
    bl_idname = "shade.dream_texture_headless"
    bl_label = "Headless Dream Texture"
    bl_description = "Generate a texture with AI"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        global timer
        return timer is None

    def invoke(self, context, event):
        weights_installed = os.path.exists(WEIGHTS_PATH)
        if not weights_installed:
            self.report({'ERROR'}, "Please complete setup in the preferences window.")
            return {'CANCELLED'}
        return self.execute(context)

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        try:
            next(generator_advance)
        except StopIteration:
            remove_timer(context)
            return {'FINISHED'}
        except Exception as e:
            remove_timer(context)
            raise e
        return {'RUNNING_MODAL'}

    def execute(self, context):
        global headless_prompt
        screen = context.screen
        scene = context.scene

        global headless_init_img
        init_img = headless_init_img or (scene.init_img if headless_prompt.use_init_img else None)

        def info(msg=""):
            scene.dream_textures_info = msg
        
        def handle_exception(fatal, msg, trace):
            info() # clear variable
            if fatal:
                kill_generator()
            self.report({'ERROR'},msg)
            if trace:
                print(trace, file=sys.stderr)
            if msg == MISSING_DEPENDENCIES_ERROR:
                from .open_latest_version import do_force_show_download
                do_force_show_download()

        def step_progress_update(self, context):
            if hasattr(context.area, "regions"):
                for region in context.area.regions:
                    if region.type == "UI":
                        region.tag_redraw()
            return None

        bpy.types.Scene.dream_textures_progress = bpy.props.IntProperty(
            name="Progress",
            default=0,
            min=0,
            max=(int(headless_prompt.strength * headless_prompt.steps) if init_img is not None else headless_prompt.steps) + 1,
            update=step_progress_update
        )
        bpy.types.Scene.dream_textures_info = bpy.props.StringProperty(name="Info", update=step_progress_update)
        
        info("Waiting For Process")
        generator = GeneratorProcess.shared()

        def save_temp_image(img, path=None):
            path = path if path is not None else tempfile.NamedTemporaryFile().name

            settings = scene.render.image_settings
            file_format = settings.file_format
            mode = settings.color_mode
            depth = settings.color_depth

            settings.file_format = 'PNG'
            settings.color_mode = 'RGBA'
            settings.color_depth = '8'

            img.save_render(path)

            settings.file_format = file_format
            settings.color_mode = mode
            settings.color_depth = depth

            return path

        if headless_prompt.use_inpainting:
            for area in screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    if area.spaces.active.image is not None:
                        init_img = area.spaces.active.image
        init_img_path = None
        if init_img is not None:
            init_img_path = save_temp_image(init_img)

        args = headless_prompt.generate_args()
        args.update(headless_args)
        args['init_img'] = init_img_path

        def step_callback(step, width=None, height=None, shared_memory_name=None):
            global headless_step_callback
            info() # clear variable
            scene.dream_textures_progress = step + 1
            headless_step_callback(step, width, height, shared_memory_name)

        def image_callback(shared_memory_name, seed, width, height, upscaled=False):
            global headless_image_callback
            info() # clear variable
            headless_image_callback(shared_memory_name, seed, width, height, upscaled)

        global generator_advance
        generator_advance = generator.prompt2image(args,
            # a function or method that will be called each step
            step_callback=step_callback,
            # a function or method that will be called each time an image is generated
            image_callback=image_callback,
            # a function or method that will recieve messages
            info_callback=info,
            exception_callback=handle_exception
        )
        context.window_manager.modal_handler_add(self)
        self.timer = context.window_manager.event_timer_add(1 / 15, window=context.window)
        return {'RUNNING_MODAL'}

def remove_timer(context):
    global timer
    if timer:
        context.window_manager.event_timer_remove(timer)
        timer = None

def kill_generator(context=bpy.context):
    GeneratorProcess.kill_shared()
    remove_timer(context)
    bpy.context.scene.dream_textures_progress = 0
    bpy.context.scene.dream_textures_info = ""
    global last_data_block
    if last_data_block is not None:
        bpy.data.images.remove(last_data_block)
        last_data_block = None

class ReleaseGenerator(bpy.types.Operator):
    bl_idname = "shade.dream_textures_release_generator"
    bl_label = "Release Generator"
    bl_description = "Releases the generator class to free up VRAM"
    bl_options = {'REGISTER'}

    def execute(self, context):
        kill_generator(context)
        return {'FINISHED'}