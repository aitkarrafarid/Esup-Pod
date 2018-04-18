from django.conf import settings
from django.core.mail import send_mail
from django.core.mail import mail_admins
from django.core.mail import mail_managers
from django.utils.translation import ugettext_lazy as _
from django.core.files.images import ImageFile
from django.core.files import File
from django.apps import apps


from pod.video.models import VideoRendition
from pod.video.models import EncodingVideo
from pod.video.models import EncodingAudio
from pod.video.models import EncodingLog
from pod.video.models import PlaylistM3U8
from pod.video.models import Video
from pod.video.models import VideoImageModel

from fractions import Fraction
from webvtt import WebVTT, Caption
import logging
import os
import time
import subprocess
import json
import re
import tempfile

if apps.is_installed('pod.filepicker'):
    try:
        from pod.filepicker.models import CustomImageModel
        from pod.filepicker.models import UserDirectory
    except ImportError:
        pass

FILEPICKER = True if apps.is_installed('pod.filepicker') else False

FFMPEG = getattr(settings, 'FFMPEG', 'ffmpeg')
FFPROBE = getattr(settings, 'FFPROBE', 'ffprobe')
DEBUG = getattr(settings, 'DEBUG', True)

log = logging.getLogger(__name__)

# try to create a new segment every X seconds
SEGMENT_TARGET_DURATION = getattr(settings, 'SEGMENT_TARGET_DURATION', 2)
# maximum accepted bitrate fluctuations
MAX_BITRATE_RATIO = getattr(settings, 'MAX_BITRATE_RATIO', 1.07)
# maximum buffer size between bitrate conformance checks
RATE_MONITOR_BUFFER_RATIO = getattr(
    settings, 'RATE_MONITOR_BUFFER_RATIO', 1.5)
# maximum threads use by ffmpeg
FFMPEG_NB_THREADS = getattr(settings, 'FFMPEG_NB_THREADS', 0)

GET_INFO_VIDEO = getattr(
    settings,
    'GET_INFO_VIDEO',
    "%(ffprobe)s -v quiet -show_format -show_streams -select_streams v:0 "
    + "-print_format json -i %(source)s")

FFMPEG_STATIC_PARAMS = getattr(
    settings,
    'FFMPEG_STATIC_PARAMS',
    " -c:a aac -ar 48000 -c:v h264 -profile:v high -pix_fmt yuv420p -crf 20 "
    + "-sc_threshold 0 -force_key_frames \"expr:gte(t,n_forced*1)\" "
    + "-deinterlace -threads %(nb_threads)s -g %(key_frames_interval)s "
    + "-keyint_min %(key_frames_interval)s ")

FFMPEG_MISC_PARAMS = getattr(settings, 'MISC_PARAMS', " -hide_banner -y ")

AUDIO_BITRATE = getattr(settings, 'AUDIO_BITRATE', "192k")

ENCODING_M4A = getattr(
    settings,
    'ENCODING_M4A',
    "%(ffmpeg)s -i %(source)s %(misc_params)s -c:a aac -b:a %(audio_bitrate)s "
    + "-vn -threads %(nb_threads)s "
    + "\"%(output_dir)s/audio_%(audio_bitrate)s.m4a\"")

ENCODE_MP3_CMD = getattr(
    settings, 'ENCODE_MP3_CMD',
    "%(ffmpeg)s -i %(source)s %(misc_params)s -vn -b:a %(audio_bitrate)s "
    + "-vn -f mp3 -threads %(nb_threads)s "
    + "\"%(output_dir)s/audio_%(audio_bitrate)s.mp3\"")

EMAIL_ON_ENCODING_COMPLETION = getattr(
    settings, 'EMAIL_ON_ENCODING_COMPLETION', True)

FILE_UPLOAD_TEMP_DIR = getattr(
    settings, 'FILE_UPLOAD_TEMP_DIR', '/tmp')


# function to store each step of encoding process
def change_encoding_step(video_id, num_step, desc):
    video_to_encode = Video.objects.get(id=video_id)
    video_to_encode.encoding_step = '{"step":%d,"desc":"%s"}' % (
        num_step, desc
    )
    video_to_encode.save()

# first function to encode file sent to Pod


def encode_video(video_id):
    start = "Start at : %s" % time.ctime()
    if DEBUG:
        print(start)

    change_encoding_step(video_id, 0, "start")

    video_to_encode = Video.objects.get(id=video_id)
    video_to_encode.encoding_in_progress = True
    video_to_encode.save()

    encoding_log = EncodingLog.objects.get_or_create(video=video_to_encode)[0]
    encoding_log.log = "%s" % start
    encoding_log.save()

    encoding_log_msg = ""

    if os.path.exists(video_to_encode.video.path):
        # PREPARE VIDEO
        source = "%s" % video_to_encode.video.path
        # get data of file sent
        change_encoding_step(video_id, 1, "Get Data from file")

        data_video = prepare_video(video_to_encode)

        encoding_log_msg += data_video["encoding_log_msg"]

        video_to_encode = Video.objects.get(id=video_id)
        video_to_encode.duration = data_video["duration"]
        video_to_encode.save()

        # LAUNCH COMMAND

        if data_video["is_video"]:

            change_encoding_step(video_id, 2, "Encoding video")
            static_params = FFMPEG_STATIC_PARAMS % {
                'nb_threads': FFMPEG_NB_THREADS,
                'key_frames_interval': data_video["key_frames_interval"]
            }
            # create video encoding command
            change_encoding_step(video_id, 2,
                                 "Encoding : create encoding command")
            video_encoding_cmd = get_video_encoding_cmd(
                static_params,
                data_video["in_height"],
                video_to_encode.id,
                data_video["output_dir"])
            encoding_log_msg += video_encoding_cmd["msg"]

            change_encoding_step(video_id, 2, "Encoding : encoding video")

            video_msg = encoding_video(
                source, video_encoding_cmd, data_video, video_to_encode)
            encoding_log_msg += video_msg

            video_360 = EncodingVideo.objects.get(
                name="360p",
                video=video_to_encode,
                encoding_format="video/mp4")

            change_encoding_step(video_id, 2, "Encoding : create overview")
            encoding_log_msg += add_overview(data_video["duration"],
                                             video_360.source_file.path,
                                             data_video["output_dir"],
                                             video_id)

            # thumbnails
            change_encoding_step(video_id, 2, "Encoding : create thumbnails")
            encoding_log_msg += add_thumbnails(
                video_360.source_file.path,
                video_id)

        else:  # not is_video:
            change_encoding_step(video_id, 2, "Encoding : encoding audio")
            encoding_log_msg += encode_m4a(
                source,
                data_video["output_dir"],
                video_to_encode)

        # generate MP3 file for all file sent
        change_encoding_step(video_id, 3, "Encoding : encoding audio")
        encoding_log_msg += encode_mp3(
            source,
            data_video["output_dir"],
            video_to_encode)

    else:  # NOT : if os.path.exists
        encoding_log_msg += "Wrong file or path : "\
            + "\n%s" % video_to_encode.video.path
        if DEBUG:
            print("Wrong file or file path :\n%s" % video_to_encode.video.path)
        # send email Alert encodage
        send_email(encoding_log_msg, video_id)

    encoding_log.log += encoding_log_msg
    encoding_log.save()

    # SEND EMAIL TO OWNER
    if EMAIL_ON_ENCODING_COMPLETION:
        send_email_encoding(video_to_encode)

    change_encoding_step(video_id, 0, "done")

    video_to_encode = Video.objects.get(id=video_id)
    video_to_encode.encoding_in_progress = False
    video_to_encode.save()


def prepare_video(video_to_encode):
    encoding_log_msg = ""
    source = "%s" % video_to_encode.video.path
    # remove previous encoding
    encoding_log_msg += remove_previous_encoding_video(
        video_to_encode)
    encoding_log_msg += remove_previous_encoding_audio(
        video_to_encode)
    encoding_log_msg += remove_previous_encoding_playlist(
        video_to_encode)

    # Get video data
    command = GET_INFO_VIDEO % {'ffprobe': FFPROBE, 'source': source}

    ffproberesult = subprocess.getoutput(command)
    info = json.loads(ffproberesult)
    encoding_log_msg += "\nffprobe command : %s" % command
    encoding_log_msg += "%s" % json.dumps(
        info, sort_keys=True, indent=4, separators=(',', ': '))

    is_video = False
    in_height = 0
    duration = 0

    if len(info["streams"]) > 0:
        is_video = True
        if info["streams"][0].get('height'):
            in_height = info["streams"][0]['height']
        if info["streams"][0]['avg_frame_rate']:
            # nb img / sec.
            avg_frame_rate = info["streams"][0]['avg_frame_rate']
            key_frames_interval = int(round(Fraction(avg_frame_rate)))

    if info["format"].get('duration'):
        duration = int(float("%s" % info["format"]['duration']))

    encoding_log_msg += "\nIN_HEIGHT : %s" % in_height
    encoding_log_msg += "\nDURATION : %s" % duration

    # CREATE OUPUT DIR
    dirname = os.path.dirname(source)
    output_dir = os.path.join(dirname, "%04d" % video_to_encode.id)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    encoding_log_msg += "\noutput_dir : %s" % output_dir

    if DEBUG:
        print("prepare_video : \n%s " % encoding_log_msg)

    return {
        'is_video': is_video,
        'output_dir': output_dir,
        'in_height': in_height,
        'duration': duration,
        'key_frames_interval': key_frames_interval,
        'encoding_log_msg': encoding_log_msg
    }


def encoding_video(source, video_encoding_cmd, data_video, video_to_encode):

    ffmpegHLScommand = "%s %s -i %s %s" % (
        FFMPEG, FFMPEG_MISC_PARAMS, source, video_encoding_cmd["cmd_hls"])
    ffmpegMP4command = "%s %s -i %s %s" % (
        FFMPEG, FFMPEG_MISC_PARAMS, source, video_encoding_cmd["cmd_mp4"])

    video_msg = "\n- ffmpegHLScommand :\n%s" % ffmpegHLScommand
    video_msg += "\n- ffmpegMP4command :\n%s" % ffmpegMP4command
    video_msg += "Encoding HLS : %s" % time.ctime()

    ffmpegvideoHLS = subprocess.getoutput(ffmpegHLScommand)

    video_msg += save_m3u8_files(
        video_encoding_cmd["list_m3u8"],
        data_video["output_dir"],
        video_to_encode,
        video_encoding_cmd["master_playlist"])

    video_msg += "\nEncoding MP4 : %s" % time.ctime()

    ffmpegvideoMP4 = subprocess.getoutput(ffmpegMP4command)

    video_msg += save_mp4_files(
        video_encoding_cmd["list_mp4"],
        data_video["output_dir"],
        video_to_encode)

    video_msg += "\nEnd Encoding video : %s" % time.ctime()

    with open(data_video["output_dir"] + "/encoding.log", "a") as f:
        f.write('\n\ffmpegvideoHLS:\n\n')
        f.write(ffmpegvideoHLS)
        f.write('\n\ffmpegvideoMP4:\n\n')
        f.write(ffmpegvideoMP4)

    if DEBUG:
        print(video_msg)

    return video_msg


def get_video_encoding_cmd(static_params, in_height, video_id, output_dir):
    msg = "\n"
    cmd_hls = ""
    cmd_mp4 = ""
    list_m3u8 = []
    list_mp4 = []

    master_playlist = "#EXTM3U\n#EXT-X-VERSION:3\n"
    renditions = VideoRendition.objects.all()
    for rendition in renditions:
        resolution = rendition.resolution
        bitrate = rendition.video_bitrate
        audiorate = rendition.audio_bitrate
        encode_mp4 = rendition.encode_mp4
        if resolution.find("x") != -1:
            width = resolution.split("x")[0]
            height = resolution.split("x")[1]
            if in_height >= int(height):

                int_bitrate = int(
                    re.search("(\d+)k", bitrate, re.I).groups()[0])
                maxrate = int_bitrate * MAX_BITRATE_RATIO
                bufsize = int_bitrate * RATE_MONITOR_BUFFER_RATIO
                bandwidth = int_bitrate * 1000

                name = "%sp" % height

                cmd = " %s -vf " % (static_params,)
                cmd += "scale=w=%s:h=%s:" % (
                    width, height)
                cmd += "force_original_aspect_ratio=decrease"
                cmd += " -b:v %s -maxrate %sk -bufsize %sk -b:a %s" % (
                    bitrate, int(maxrate), int(bufsize), audiorate)
                cmd_hls += cmd + " -hls_playlist_type vod -hls_time %s \
                    -hls_flags single_file %s/%s.m3u8" % (
                    SEGMENT_TARGET_DURATION, output_dir, name)
                list_m3u8.append(
                    {"name": name, 'rendition': rendition})

                if encode_mp4:
                    # encode only in mp4
                    cmd_mp4 += cmd + \
                        " -movflags faststart -write_tmcd 0 \"%s/%s.mp4\"" % (
                            output_dir, name)
                    list_mp4.append(
                        {"name": name, 'rendition': rendition})
                master_playlist += "#EXT-X-STREAM-INF:BANDWIDTH=%s,\
                    RESOLUTION=%s\n%s.m3u8\n" % (
                    bandwidth, resolution, name)
        else:
            msg += "\nerror in resolution %s" % resolution
            send_email(msg, video_id)
            if DEBUG:
                print("Error in resolution %s" % resolution)
    return {
        'cmd_hls': cmd_hls,
        'cmd_mp4': cmd_mp4,
        'list_m3u8': list_m3u8,
        'list_mp4': list_mp4,
        'master_playlist': master_playlist,
        'msg': msg
    }


def encode_m4a(source, output_dir, video_to_encode):
    msg = "\nEncoding M4A : %s" % time.ctime()
    command = ENCODING_M4A % {
        'ffmpeg': FFMPEG,
        'source': source,
        'misc_params': FFMPEG_MISC_PARAMS,
        'nb_threads': FFMPEG_NB_THREADS,
        'output_dir': output_dir,
        'audio_bitrate': AUDIO_BITRATE
    }
    ffmpegaudio = subprocess.getoutput(command)
    if os.access(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE, os.F_OK):
        if (os.stat(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE).st_size > 0):
            encoding, created = EncodingAudio.objects.get_or_create(
                name="audio",
                video=video_to_encode,
                encoding_format="video/mp4")
            encoding.source_file = output_dir.replace(
                settings.MEDIA_ROOT + '/', '')
            + "/audio_%s.m4a" % AUDIO_BITRATE
            encoding.save()
        else:
            os.remove(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE)
            msg += "\nERROR ENCODING M4A audio_%s.m4a "
            +"Output size is 0" % AUDIO_BITRATE
            log.error(msg)
            send_email(msg, video_to_encode.id)
    else:
        msg += "\nERROR ENCODING M4A audio_%s.m4a "\
            + "DOES NOT EXIST" % AUDIO_BITRATE
        log.error(msg)
        send_email(msg, video_to_encode.id)

    with open(output_dir + "/encoding.log", "a") as f:
        f.write('\n\nffmpegaudio:\n\n')
        f.write(ffmpegaudio)
    if DEBUG:
        print(msg)
    return msg


def encode_mp3(source, output_dir, video_to_encode):
    msg = "\nEncoding MP3 : %s" % time.ctime()
    command = ENCODE_MP3_CMD % {
        'ffmpeg': FFMPEG,
        'source': source,
        'misc_params': FFMPEG_MISC_PARAMS,
        'nb_threads': FFMPEG_NB_THREADS,
        'output_dir': output_dir,
        'audio_bitrate': AUDIO_BITRATE
    }
    ffmpegaudiomp3 = subprocess.getoutput(command)
    if os.access(output_dir + "/audio_%s.mp3" % AUDIO_BITRATE, os.F_OK):
        if (os.stat(output_dir + "/audio_%s.mp3" % AUDIO_BITRATE).st_size > 0):
            encoding, created = EncodingAudio.objects.get_or_create(
                name="audio",
                video=video_to_encode,
                encoding_format="audio/mp3")
            encoding.source_file = output_dir.replace(
                settings.MEDIA_ROOT + '/', '')\
                + "/audio_%s.mp3" % AUDIO_BITRATE
            encoding.save()
        else:
            os.remove(output_dir + "/audio_%s.m4a" % AUDIO_BITRATE)
            msg += "\nERROR ENCODING M4A audio_%s.m4a " % AUDIO_BITRATE\
                + "Output size is 0"
            log.error(msg)
            send_email(msg, video_to_encode.id)
    else:
        msg += "\nERROR ENCODING M4A audio_%s.m4a" % AUDIO_BITRATE
        msg += " DOES NOT EXIST"
        log.error(msg)
        send_email(msg, video_to_encode.id)

    with open(output_dir + "/encoding.log", "a") as f:
        f.write('\n\ffmpegaudiomp3:\n\n')
        f.write(ffmpegaudiomp3)
    if DEBUG:
        print(msg)
    return msg


def save_m3u8_files(list_m3u8, output_dir, video_to_encode, master_playlist):
    msg = "\n"
    for m3u8 in list_m3u8:
        # check size of file
        videofilenameM3u8 = os.path.join(output_dir, "%s.m3u8" % m3u8['name'])
        videofilenameTS = os.path.join(output_dir, "%s.ts" % m3u8['name'])
        msg += "\n- videofilenameM3u8 :\n%s" % videofilenameM3u8
        msg += "\n- videofilenameTS :\n%s" % videofilenameTS

        if (os.access(videofilenameM3u8, os.F_OK)
                and os.access(videofilenameTS, os.F_OK)):
            # There was a error cause the outfile size is zero
            if (os.stat(videofilenameTS).st_size > 0
                    and os.stat(videofilenameM3u8).st_size > 0):
                # save file in bdd
                encoding, created = EncodingVideo.objects.get_or_create(
                    name=m3u8['name'],
                    video=video_to_encode,
                    rendition=m3u8['rendition'],
                    encoding_format="video/mp2t")
                encoding.source_file = videofilenameTS.replace(
                    settings.MEDIA_ROOT + '/', '')
                encoding.save()

                playlistM3U8, created = PlaylistM3U8.objects.get_or_create(
                    name=m3u8['name'],
                    video=video_to_encode,
                    encoding_format="application/x-mpegURL")
                playlistM3U8.source_file = videofilenameM3u8.replace(
                    settings.MEDIA_ROOT + '/', '')
                playlistM3U8.save()

            else:
                msg += "\nERROR ENCODING M3U8 %s Output size is 0" % m3u8[
                    'name']
                log.error(msg)
                send_email(msg, video_to_encode.id)
        else:
            msg += "\nERROR ENCODING M3U8 %s DOES NOT EXIST" % m3u8['name']
            log.error(msg)
            send_email(msg, video_to_encode.id)

    # PLAYLIST
    with open(output_dir + "/playlist.m3u8", "w") as f:
        f.write(master_playlist)
    if os.access(output_dir + "/playlist.m3u8", os.F_OK):
        if (os.stat(output_dir + "/playlist.m3u8").st_size > 0):

            playlistM3U8, created = PlaylistM3U8.objects.get_or_create(
                name="playlist",
                video=video_to_encode,
                encoding_format="application/x-mpegURL")
            playlistM3U8.source_file = output_dir.replace(
                settings.MEDIA_ROOT + '/', '') + "/playlist.m3u8"
            playlistM3U8.save()

            msg += "\n- Playlist :\n%s" % output_dir + \
                "/playlist.m3u8"
        else:
            os.remove(output_dir + "/playlist.m3u8")
            msg += "\nERROR ENCODING PLAYLIST M3U8 %s Output size is 0" % m3u8[
                'name']
            log.error(msg)
            send_email(msg, video_to_encode.id)
    else:
        msg += "\nERROR ENCODING M3U8 %s DOES NOT EXIST" % m3u8['name']
        log.error(msg)
        send_email(msg, video_to_encode.id)

    return msg


def save_mp4_files(list_mp4, output_dir, video_to_encode):
    msg = "\n"
    for mp4 in list_mp4:
        # check size of file
        videofilename = os.path.join(output_dir, "%s.mp4" % mp4['name'])
        msg += "\n- videofilename :\n%s" % videofilename
        if os.access(videofilename, os.F_OK):  # outfile exists
            # There was a error cause the outfile size is zero
            if (os.stat(videofilename).st_size > 0):
                # save file in bdd
                encoding, created = EncodingVideo.objects.get_or_create(
                    name=mp4['name'],
                    video=video_to_encode,
                    rendition=mp4['rendition'],
                    encoding_format="video/mp4")
                encoding.source_file = videofilename.replace(
                    settings.MEDIA_ROOT + '/', '')
                encoding.save()
            else:
                os.remove(videofilename)
                msg += "\nERROR ENCODING MP4 %s Output size is 0" % mp4[
                    'name']
                log.error(msg)
                send_email(msg, video_to_encode.id)
        else:
            msg += "\nERROR ENCODING MP4 %s DOES NOT EXIST" % mp4['name']
            log.error(msg)
            send_email(msg, video_to_encode.id)
    if DEBUG:
        print(msg)
    return msg
###############################################################
# THUMBNAILS
###############################################################
# nice -19 ffmpegthumbnailer -i \"%(src)s\" -s 256x256 -t 10%% -o
# %(out)s_2.png && nice -19 ffmpegthumbnailer -i \"%(src)s\" -s 256x256 -t
# 50%% -o %(out)s_3.png && nice -19 ffmpegthumbnailer -i \"%(src)s\" -s
# 256x256 -t 75%% -o %(out)s_4.png"


def add_thumbnails(source, video_id):
    msg = "\nCREATE THUMBNAILS : %s" % time.ctime()
    tempimgfile = tempfile.NamedTemporaryFile(
        dir=FILE_UPLOAD_TEMP_DIR, suffix='')
    image_width = 360  # default size of image
    msg += "\ncreate thumbnails image file"
    for i in range(0, 3):
        percent = str((i + 1) * 25) + "%"
        cmd_ffmpegthumbnailer = "ffmpegthumbnailer -t \"%(percent)s\" \
        -s \"%(image_width)s\" -i %(source)s -c png \
        -o %(tempfile)s_%(num)s.png" % {
            "percent": percent,
            'source': source,
            'num': i,
            'image_width': image_width,
            'tempfile': tempimgfile.name
        }
        subprocess.getoutput(cmd_ffmpegthumbnailer)
        thumbnailfilename = "%(tempfile)s_%(num)s.png" % {
            'num': i,
            'tempfile': tempimgfile.name
        }
        if os.access(thumbnailfilename, os.F_OK):  # outfile exists
            # There was a error cause the outfile size is zero
            if (os.stat(thumbnailfilename).st_size > 0):
                if FILEPICKER:
                    video_to_encode = Video.objects.get(id=video_id)
                    homedir, created = UserDirectory.objects.get_or_create(
                        name='home',
                        owner=video_to_encode.owner.user,
                        parent=None)
                    videodir, created = UserDirectory.objects.get_or_create(
                        name='%s' % video_to_encode.slug,
                        owner=video_to_encode.owner.user,
                        parent=homedir)
                    thumbnail = CustomImageModel(directory=videodir)
                    thumbnail.file.save(
                        "%d_%s.png" % (video_id, i),
                        File(open(thumbnailfilename)),
                        save=True)
                    thumbnail.save()
                else:
                    thumbnail = VideoImageModel()
                    thumbnail.file.save(
                        "%d_%s.png" % (video_id, i),
                        File(open(thumbnailfilename)),
                        save=True)
                    thumbnail.save()

                    video_to_encode = Video.objects.get(id=video_id)
                    video_to_encode.thumbnail = thumbnail
                    video_to_encode.save()

            else:
                os.remove(thumbnailfilename)
                msg += "\nERROR THUMBNAILS %s " % thumbnailfilename
                msg += "Output size is 0"
                log.error(msg)
                send_email(msg, video_id)
        else:
            msg += "\nERROR THUMBNAILS %s DOES NOT EXIST" % thumbnailfilename
            log.error(msg)
            send_email(msg, video_id)
    if DEBUG:
        print(msg)
    return msg

###############################################################
# OVERVIEW
###############################################################


def remove_old_overview_file(overviewfilename, overviewimagefilename):
    if os.path.isfile(overviewimagefilename):
        os.remove(overviewimagefilename)
    if os.path.isfile(overviewfilename):
        os.remove(overviewfilename)


def add_overview(duration, source, output_dir, video_id):
    msg = "\nCREATE OVERVIEW : %s" % time.ctime()
    overviewfilename = '%(output_dir)s/overview.vtt' % {
        'output_dir': output_dir}
    image_url = 'overview.png'
    overviewimagefilename = '%(output_dir)s/%(image_url)s' % {
        'output_dir': output_dir, 'image_url': image_url}
    image_width = 180  # width of generate image file

    remove_old_overview_file(overviewfilename, overviewimagefilename)

    # create overviewimagefilename
    msg += "\ncreate overview image file"
    for i in range(0, 99):
        percent = "%s" % i
        percent += "%"
        cmd_ffmpegthumbnailer = "ffmpegthumbnailer -t \"%(percent)s\" \
        -s \"%(image_width)s\" -i %(source)s -c png \
        -o %(source)s_strip%(num)s.png" % {
            "percent": percent,
            'source': source,
            'num': i,
            'image_width': image_width
        }
        subprocess.getoutput(cmd_ffmpegthumbnailer)
        cmd_montage = "montage -geometry +0+0 %(output_dir)s/overview.png \
        %(source)s_strip%(num)s.png %(output_dir)s/overview.png" % {
            "percent": percent + "%",
            'source': source,
            'num': i,
            'output_dir': output_dir
        }
        subprocess.getoutput(cmd_montage)
        if os.path.isfile("%(source)s_strip%(num)s.png" % {
            'source': source,
            'num': i
        }):
            os.remove("%(source)s_strip%(num)s.png" %
                      {'source': source, 'num': i})

    # create overview vtt
    msg += "\ncreate overview vtt file"
    # get image size
    overview = ImageFile(open(overviewimagefilename, 'rb'))
    image_height = int(overview.height)
    overview.close()
    # creating webvtt file
    webvtt = WebVTT()
    for i in range(0, 99):
        start = format(float(duration * i / 100), '.3f')
        end = format(float(duration * (i + 1) / 100), '.3f')
        start_time = time.strftime(
            '%H:%M:%S',
            time.gmtime(int(str(start).split('.')[0]))
        )
        start_time += ".%s" % (str(start).split('.')[1])
        end_time = time.strftime('%H:%M:%S', time.gmtime(
            int(str(end).split('.')[0]))) + ".%s" % (str(end).split('.')[1])
        caption = Caption(
            '%s' % start_time,
            '%s' % end_time,
            '%s#xywh=%d,%d,%d,%d' % (
                image_url, image_width * i, 0, image_width, image_height)
        )
        webvtt.captions.append(caption)
    webvtt.save(overviewfilename)

    # record in Video model
    msg += "\nstore vtt file in bdd with video model overview field"
    if os.access(overviewfilename, os.F_OK):  # outfile exists
        # There was a error cause the outfile size is zero
        if (os.stat(overviewfilename).st_size > 0):
            # save file in bdd
            video_to_encode = Video.objects.get(id=video_id)
            video_to_encode.overview = overviewfilename.replace(
                settings.MEDIA_ROOT + '/', '')
            video_to_encode.save()
        else:
            os.remove(overviewfilename)
            msg += "\nERROR OVERVIEW %s Output size is 0" % overviewfilename
            log.error(msg)
            send_email(msg, video_id)
    else:
        msg += "\nERROR OVERVIEW %s DOES NOT EXIST" % overviewfilename
        log.error(msg)
        send_email(msg, video_id)

    if DEBUG:
        print(msg)

    return msg

###############################################################
# REMOVE ENCODING
###############################################################


def remove_previous_encoding_video(video_to_encode):
    msg = "\n"
    # Remove previous encoding Video
    previous_encoding_video = EncodingVideo.objects.filter(
        video=video_to_encode)
    if len(previous_encoding_video) > 0:
        if DEBUG:
            print("DELETE PREVIOUS ENCODING")

        msg += "\nDELETE PREVIOUS ENCODING VIDEO"
        # previous_encoding.delete()
        for encoding in previous_encoding_video:
            encoding.delete()
    return msg


def remove_previous_encoding_audio(video_to_encode):
    msg = "\n"
    # Remove previous encoding Audio
    previous_encoding_audio = EncodingAudio.objects.filter(
        video=video_to_encode)
    if len(previous_encoding_audio) > 0:
        if DEBUG:
            print("DELETE PREVIOUS ENCODING")

        msg += "\nDELETE PREVIOUS ENCODING AUDIO"
        # previous_encoding.delete()
        for encoding in previous_encoding_audio:
            encoding.delete()
    return msg


def remove_previous_encoding_playlist(video_to_encode):
    msg = "\n"
    # Remove previous encoding Playlist
    previous_playlist = PlaylistM3U8.objects.filter(video=video_to_encode)
    if len(previous_playlist) > 0:
        if DEBUG:
            print("DELETE PREVIOUS PLAYLIST M3U8")

        msg += "DELETE PREVIOUS PLAYLIST M3U8"
        # previous_encoding.delete()
        for encoding in previous_playlist:
            encoding.delete()

    return msg


###############################################################
# EMAIL
###############################################################


def send_email(msg, video_id):
    subject = "[" + settings.TITLE_SITE + \
        "] Error Encoding Video id:%s" % video_id
    message = "Error Encoding  video id : %s\n%s" % (
        video_id, msg)
    html_message = "<p>Error Encoding video id : %s</p><p>%s</p>" % (
        video_id,
        msg.replace('\n', "<br/>"))
    mail_admins(
        subject,
        message,
        fail_silently=False,
        html_message=html_message)


def send_email_encoding(video_to_encode):
    if DEBUG:
        print("SEND EMAIL ON ENCODING COMPLETION")
    content_url = video_to_encode.get_absolute_url()
    subject = "[%s] %s" % (
        settings.TITLE_SITE,
        _(u"Encoding #%(content_id)s completed") % {
            'content_id': video_to_encode.id
        }
    )
    message = "%s\n\n%s\n%s\n" % (
        _(u"The content “%(content_title)s” has been encoded to Web "
            + "formats, and is now available on %(site_title)s.") % {
            'content_title': video_to_encode.title,
            'site_title': settings.TITLE_SITE
        },
        _(u"You will find it here:"),
        content_url
    )
    from_email = settings.DEFAULT_FROM_EMAIL
    to_email = []
    to_email.append(video_to_encode.owner.email)
    html_message = ""

    html_message = '<p>%s</p><p>%s<br><a href="%s"><i>%s</i></a>\
                </p>' % (
        _(u"The content “%(content_title)s” has been encoded to Web "
            + "formats, and is now available on %(site_title)s.") % {
            'content_title': '<b>%s</b>' % video_to_encode.title,
            'site_title': settings.TITLE_SITE
        },
        _(u"You will find it here:"),
        content_url,
        content_url
    )
    send_mail(
        subject,
        message,
        from_email,
        to_email,
        fail_silently=False,
        html_message=html_message,
    )
    mail_managers(
        subject, message, fail_silently=False,
        html_message=html_message)
