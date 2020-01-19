import logging
from pajbot.managers.db import DBManager
from pajbot.models.songrequest import SongrequestQueue, SongrequestHistory
from pajbot.managers.schedule import ScheduleManager
import threading
import time

current_milli_time = lambda: int(round(time.time() * 10))
log = logging.getLogger("pajbot")
WIDGET_ID = "4"


class SongrequestManager:
    def __init__(self, bot):
        self.bot = bot
        self.enabled = False
        self.current_song_id = None

    def enable(self, settings, youtube):
        self.enabled = True
        self.showVideo = False
        self.isVideoShowing = True
        self.youtube = youtube
        self.settings = settings
        self.current_song_id = None
        self.previously_playing_spotify = False
        self.paused = False
        self.module_opened = False
        self.previous_queue = 0
        self.volume = self.settings["volume"] / 100
        self.scheduled_tasks = []
        thread = threading.Thread(target=self.inc_current_song, daemon=True)
        thread.start()

    def disable(self):
        self.enabled = False
        self.paused = False
        self.settings = None
        self.youtube = None
        self.current_song_id = None
        self.module_opened = False

    def open_module_function(self):
        if not self.enabled:
            return False
        if not self.module_opened:
            self.module_opened = True
            self.paused = False
            if not self.current_song_id:
                self.load_song()
            return True
        return False

    def close_module_function(self):
        if not self.enabled:
            return False
        if self.module_opened:
            self.module_opened = False
            self.paused = False
            return True
        return False

    def skip_function(self, skipped_by):
        if not self.enabled and self.current_song_id:
            return False
        self.load_song(skipped_by)
        return True

    def previous_function(self, requested_by):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            SongrequestHistory._insert_previous(db_session, requested_by, self.previous_queue)
            db_session.commit()
        self.previous_queue += 1
        self.load_song(requested_by)
        return True

    def pause_function(self):
        if not self.enabled or not self.current_song_id:
            return False
        if not self.paused:
            self.paused = True
            self._pause()
            return True
        return False

    def resume_function(self):
        if not self.enabled or not self.current_song_id:
            return False
        if self.paused:
            self.paused = False
            self._resume()
            if not self.current_song_id and self.module_opened:
                self.load_song()
            return True
        return False

    def seek_function(self, time):
        if not self.enabled:
            return False
        if self.current_song_id:
            with DBManager.create_session_scope() as db_session:
                current_song = SongrequestQueue._from_id(db_session, self.current_song_id)
                current_song.current_song_time = time
                self._seek(time)
            return True
        return False

    def volume_function(self, volume):
        if not self.enabled:
            return False
        self.volume = volume * (self.settings["volume_multiplier"] / 100)
        self._volume()
        return True

    def play_function(self, database_id, skipped_by):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            song = SongrequestQueue._from_id(db_session, database_id)
            song._move_song(db_session, 1)
            db_session.commit()
        self.load_song(skipped_by)
        SongrequestQueue._update_queue()
        return True

    def move_function(self, database_id, to_id):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            song = SongrequestQueue._from_id(db_session, database_id)
            song._move_song(db_session, to_id)
            db_session.commit()
        self._playlist()
        SongrequestQueue._update_queue()
        return True

    def request_function(self, video_id, requested_by):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            song_info = SongRequestSongInfo._create_or_get(db_session, video_id, self.youtube)
            if not song_info:
                log.error("There was an error!")
                return True
            skip_after = (
                self.settings["max_song_length"] if song_info.duration > self.settings["max_song_length"] else None
            )
            songrequest_queue = SongrequestQueue._create(db_session, video_id, skip_after, source.login)
            db_session.commit()
        SongrequestQueue._update_queue()
        return True

    def replay_function(self, requested_by):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            current_song = SongrequestQueue._from_id(db_session, self.current_song_id)
            self.request_function(current_song.video_id, current_song.requested_by)._move_song(db_session, 1)
            db_session.commit()
        self.load_song(requested_by)
        SongrequestQueue._update_queue()
        return True

    def requeue_function(self, database_id, requested_by):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            SongrequestHistory._from_id(db_session, database_id).requeue(db_session, requested_by)
            db_session.commit()
        SongrequestQueue._update_queue()
        self._playlist()
        return True

    def show_function(self):
        if not self.enabled:
            return False
        if not self.showVideo:
            self.showVideo = True
            if not self.paused:
                self._show()
            return True
        return False

    def hide_function(self):
        if not self.enabled:
            return False
        if self.showVideo:
            self.showVideo = False
            self._hide()
            return True
        return False

    def remove_function(self, database_id):
        if not self.enabled:
            return False
        with DBManager.create_session_scope() as db_session:
            song = SongrequestQueue._from_id(db_session, database_id)
            song._remove(db_session)
            db_session.commit()
        SongrequestQueue._update_queue()
        self._playlist()
        return True

    def inc_current_song(self):
        while True:
            if not self.enabled:
                break
            if self.current_song_id:
                if not self.paused:
                    try:
                        with DBManager.create_session_scope() as db_session:
                            current_song = SongrequestQueue._from_id(db_session, self.current_song_id)
                            next_song = SongrequestQueue._get_next_song(db_session)
                            if not current_song:
                                self.load_song()
                            else:
                                if (
                                    (not current_song.requested_by or current_song.requested_by == "Backup Playlist")
                                    and next_song
                                    and next_song.requested_by
                                    and next_song.requested_by != "Backup Playlist"
                                ):
                                    self.load_song("Backup Playlist Skip")
                                elif current_song.current_song_time >= current_song.duration(db_session):
                                    self.load_song()
                                current_song.current_song_time += 1
                    except:
                        pass
            elif self.module_opened:
                self.load_song()
            time.sleep(1)

    def load_song(self, skipped_by=None):
        if not self.enabled:
            return False
        if self.current_song_id:
            with DBManager.create_session_scope() as db_session:
                current_song = SongrequestQueue._from_id(db_session, self.current_song_id)
                if current_song:
                    if current_song.current_song_time > 5:
                        self.previous_queue = 0
                        histroy = current_song._to_histroy(db_session, skipped_by)
                        if not histroy:
                            log.info("Something went wrong changing song queue to song history")
                            return False

                    else:
                        current_song._remove(db_session)
                self._stop_video()
                self._hide()
                db_session.commit()
        self._playlist_history()
        SongrequestQueue._update_queue()

        self.current_song_id = None
        for job in self.scheduled_tasks:
            try:
                job.remove()
            except:
                pass
        self.scheduled_tasks = []

        if not self.module_opened:
            return False

        with DBManager.create_session_scope() as db_session:
            current_song = SongrequestQueue._get_current_song(db_session)
            if not current_song:
                current_song = SongrequestQueue._get_next_song(db_session)
            if current_song:
                current_song.playing = True
                current_song.queue = 0
                current_song.current_song_time = 0
                self.current_song_id = current_song.id
                song_info = current_song.song_info(db_session)
                self._play(current_song.video_id, song_info.title, current_song.requested_by)
                if self.settings["use_spotify"]:
                    is_playing, song_name, artistsArr = self.bot.spotify_api.state(self.bot.spotify_token_manager)
                    if is_playing:
                        self.bot.spotify_api.pause(self.bot.spotify_token_manager)
                        self.previously_playing_spotify = True
                if not current_song.requested_by or current_song.requested_by == "Backup Playlist":
                    SongrequestQueue._create(
                        db_session,
                        current_song.video_id,
                        current_song.skip_after,
                        None,
                        SongrequestQueue._get_next_queue(db_session),
                    )
                db_session.commit()
                self._playlist()
                SongrequestQueue._update_queue()
                return True
            if self.settings["use_spotify"]:
                if self.previously_playing_spotify:
                    self.bot.spotify_api.play(self.bot.spotify_token_manager)
                    self.previously_playing_spotify = False
            if self.isVideoShowing:
                self._hide()
        return False

    def _play(self, video_id, video_title, requested_by):
        self.bot.websocket_manager.emit(
            "songrequest_play", WIDGET_ID, {"video_id": video_id,},
        )
        self.bot.songrequest_websocket_manager.emit(
            "play", {"video_id": video_id, "video_title": video_title, "requested_by": requested_by,},
        )
        self.paused = True
        self.scheduled_tasks.append(ScheduleManager.execute_delayed(3, self.resume_function))
        self.scheduled_tasks.append(ScheduleManager.execute_delayed(3, self._volume))
        if self.showVideo:
            self._show()
        self._playlist()

    def _pause(self):
        self.bot.websocket_manager.emit(
            "songrequest_pause", WIDGET_ID, {},
        )
        self.bot.songrequest_websocket_manager.emit(
            "pause", {},
        )
        self._hide()

    def _resume(self):
        self.bot.websocket_manager.emit(
            "songrequest_resume", WIDGET_ID, {},
        )
        self.bot.songrequest_websocket_manager.emit(
            "resume", {},
        )
        self.paused = False
        if self.showVideo:
            self._show()

    def _volume(self):
        self.bot.websocket_manager.emit(
            "songrequest_volume", WIDGET_ID, {"volume": self.volume * 100,},
        )
        self.bot.songrequest_websocket_manager.emit(
            "volume", {"volume": self.volume * 100 * (1 / (self.settings["volume_multiplier"] / 100)),},
        )

    def _seek(self, time):
        self.bot.websocket_manager.emit(
            "songrequest_seek", WIDGET_ID, {"seek_time": time,},
        )
        self.bot.songrequest_websocket_manager.emit(
            "seek", {"seek_time": time,},
        )
        self.paused = True
        self.scheduled_tasks.append(ScheduleManager.execute_delayed(1, self.resume_function))

    def _show(self):
        self.bot.websocket_manager.emit(
            "songrequest_show", WIDGET_ID, {},
        )
        self.isVideoShowing = True

    def _hide(self):
        self.bot.websocket_manager.emit(
            "songrequest_hide", WIDGET_ID, {},
        )
        self.isVideoShowing = False

    def _playlist(self):
        with DBManager.create_session_scope() as db_session:
            self.bot.songrequest_websocket_manager.emit(
                "playlist", {"playlist": SongrequestQueue._get_playlist(db_session, 15),},
            )

    def _playlist_history(self):
        with DBManager.create_session_scope() as db_session:
            self.bot.songrequest_websocket_manager.emit(
                "history", {"history": SongrequestHistory._get_history(db_session, 15),},
            )

    def _stop_video(self):
        self.bot.websocket_manager.emit(
            "songrequest_stop", WIDGET_ID, {},
        )
        self.bot.songrequest_websocket_manager.emit(
            "stop", {},
        )