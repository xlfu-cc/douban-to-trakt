#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright 2023-2023 xlfu.cc <xlfu.cc@gmail.com>. All Rights Reserved.
# License:  GNU General Public License version 3 or later; see LICENSE.txt
# Website:  https://trakt.tv, https://github.com/xlfu-cc/douban-to-trakt.git
#
import csv
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import time
from itertools import groupby
import requests
from tqdm import trange

import yaml
from trakt import Trakt
from trakt.core.pagination import PaginationIterator
from trakt.objects import Episode, Movie, Season, Show

from file import WorkingDir
from logger import logger


def split(data_list, prediction):
    left = list(filter(lambda x: prediction(x), data_list))
    right = list(filter(lambda x: not prediction(x), data_list))
    return left, right


class LocalItem:
    @classmethod
    def key(cls, item):
        media_type = item["media_type"]
        if media_type in ["movie", "show"]:
            return "{}-{}".format(media_type, item["trakt_id"])
        elif media_type == "season":
            return "season-{}-s{}".format(item["trakt_show_id"], item["season_number"])
        elif media_type == "episode":
            if item.get("episode_number") is not None:
                return "episode-{}-s{}e{}".format(item["trakt_show_id"], item["season_number"], item["episode_number"])
            else:
                raise Exception('Get key error: episode without "episode_number"!')
        else:
            raise Exception(f"Get key error: unknown media_type {media_type}")

    @classmethod
    def validate_id(cls, item):
        return bool(item.get("trakt_id"))

    @classmethod
    def validate_id_date(cls, item):
        return cls.validate_id(item) and item["date"]

    @classmethod
    def validate_id_date_rating(cls, item):
        return cls.validate_id_date(item) and item["rating"]

    @classmethod
    def validate_id_comment(cls, item):
        # TODO created_at?
        return cls.validate_id(item) and item["comment"]

    @classmethod
    def data_id(cls, item):
        if item.get("trakt_id"):
            return {"ids": {"trakt": item["trakt_id"]}}
        else:
            return {"ids": {"imdb": item["imdb_id"]}}

    @classmethod
    def data_id_watched(cls, item):
        data = cls.data_id(item)
        # set to 9:00 AM
        data["watched_at"] = cls._to_utc_time_string(item["date"], 9)
        return data

    @classmethod
    def data_id_rating(cls, item):
        data = cls.data_id(item)
        # set to 9:00 AM
        data["rated_at"] = cls._to_utc_time_string(item["date"], 9)
        data["rating"] = int(item["rating"]) * 2
        return data

    @classmethod
    def data_id_comment(cls, item):
        # set to 9:00 AM
        # TODO created_at?
        return {
            item["media_type"]: cls.data_id(item),
            "spoiler": False,
            "comment": "{}, watched at {}, imported from douban".format(item["comment"], item["date"]),
        }

    @classmethod
    def reset_trakt_info(cls, item):
        item["trakt_id"] = None
        item["media_type"] = None
        item["trakt_show_id"] = None
        item["season_number"] = None
        item["trakt_episode_ids"] = None
        item["candidates"] = None

    @classmethod
    def typed_string(cls, items):
        total_cnt = len(items)
        if total_cnt == 0:
            return "0 items"
        else:
            details = ""
            for media_type in ["movie", "show", "season", "episode"]:
                cnt = len(list(filter(lambda x: x["media_type"] == media_type, items)))
                details = f"{details}, {cnt} {media_type}s" if cnt else details
            return f"{total_cnt}({details[2:]}) items"

    @classmethod
    def segment_data(cls, items, item_info, segment_size):
        segment_data = []
        sorted_items = sorted(items, key=lambda x: x["media_type"])
        # split by segment_size
        segments = [sorted_items[i : i + segment_size] for i in range(0, len(sorted_items), segment_size)]
        for segment in segments:
            # group by type
            grouped = [list(group) for _, group in groupby(segment, lambda x: x["media_type"])]
            segment_data.append(dict([(f"{group[0]['media_type']}s", list(item_info(x) for x in group)) for group in grouped]))
        return segment_data

    @classmethod
    def to_string(cls, item):
        return "{} - {}".format(item["imdb_id"], item["title"])

    @classmethod
    def to_string_with_comment(cls, item):
        return "{} - {} : {}".format(item["imdb_id"], item["title"], item["comment"])

    @classmethod
    def _to_utc_time_string(cls, date_string, hours_offset):
        """
        time format: "2014-09-01T09:10:11.000Z"
        """
        date = datetime.strptime(date_string, "%Y-%m-%d") + timedelta(hours=hours_offset)
        return "{}Z".format(date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3])


class TraktItem:
    @classmethod
    def key(cls, item):
        if isinstance(item, Movie):
            return "movie-{}".format(item.get_key("trakt"))
        elif isinstance(item, dict) and item.get("type") == "movie":
            return "movie-{}".format(item["movie"]["ids"]["trakt"])
        elif isinstance(item, Show):
            return "show-{}".format(item.get_key("trakt"))
        elif isinstance(item, Season):
            return "season-{}-s{}".format(item.show.get_key("trakt"), item.pk)
        elif isinstance(item, dict) and item.get("type") == "season":
            return "season-{}-s{}".format(item["show"]["ids"]["trakt"], item["season"]["number"])
        elif isinstance(item, Episode):
            return "episode-{}-s{}e{}".format(item.season.show.get_key("trakt"), item.pk[0], item.pk[1])
        else:
            raise Exception(f"Get key error: unknown type {type(item)}")

    @classmethod
    def get_trakt_id(cls, item):
        if isinstance(item, Movie) or isinstance(item, Show):
            return item.get_key("trakt")
        elif isinstance(item, Season) or isinstance(item, Episode):
            return item.to_dict()["ids"]["trakt"]
        else:
            raise Exception("Unknown media {}".format(item))

    @classmethod
    def type_name(cls, item):
        if isinstance(item, dict):
            return item["type"]
        else:
            return type(item).__name__.lower()

    @classmethod
    def link(cls, item):
        if isinstance(item, Movie):
            return "https://trakt.tv/movies/{}".format(item.get_key("slug"))
        elif isinstance(item, Show):
            return "https://trakt.tv/shows/{}".format(item.get_key("slug"))
        elif isinstance(item, Season):
            return "https://trakt.tv/shows/{}/seasons/{}".format(item.show.get_key("slug"), item.pk)
        elif isinstance(item, Episode):
            return "https://trakt.tv/shows/{}/seasons/{}/episodes/{}".format(item.show.get_key("slug"), item.pk[0], item.pk[1])
        return "Unknown type {}".format(type(item))

    @classmethod
    def segment_data(cls, items, segment_size):
        segment_data = []
        sorted_items = sorted(items, key=lambda x: cls.type_name(x))
        # split by segment_size
        segments = [sorted_items[i : i + segment_size] for i in range(0, len(sorted_items), segment_size)]
        for segment in segments:
            # group by type
            grouped = [list(group) for _, group in groupby(segment, lambda x: cls.type_name(x))]
            # get ids
            segment_data.append(dict([(cls.type_name(group[0]) + "s", list({"ids": x.to_dict()["ids"]} for x in group)) for group in grouped]))
        return segment_data

    @classmethod
    def flat_to_seasons(cls, items):
        result = []

        def add_item(_item):
            if isinstance(_item, Show):
                for season in _item.seasons.values():
                    if not season.show:
                        season.show = _item
                    add_item(season)
            elif isinstance(_item, Season) or isinstance(_item, Episode) or isinstance(_item, Movie):
                result.append(_item)

        for item in items.values():
            add_item(item)
        return sorted(result, key=lambda x: cls.type_name(x))

    @classmethod
    def typed_string(cls, items):
        sorted_items = sorted(items, key=lambda x: cls.type_name(x))
        grouped = dict((f"{k}s", list(g)) for k, g in groupby(sorted_items, lambda x: cls.type_name(x)))
        return cls.typed_string_for_grouped(grouped)

    @classmethod
    def typed_string_for_grouped(cls, grouped):
        total_cnt = 0
        details = ""
        for media_type in ["movies", "shows", "seasons", "episodes"]:
            cnt = len(grouped[media_type]) if media_type in grouped else 0
            total_cnt += cnt
            details = f"{details}, {cnt} {media_type}" if cnt else details
        return f"{total_cnt}({details[2:]}) items" if total_cnt else "0 items"

    @classmethod
    def to_string(cls, item):
        if isinstance(item, dict):
            return "{} - {}".format(cls.type_name(item), item)
        else:
            return "{} - {} - {}".format(cls.type_name(item), item.get_key("trakt"), cls.link(item))


class LocalSource:
    def __init__(self, csv_file, trakt):
        self.csv_file = csv_file
        self.trakt: TraktSource = trakt
        self.items = None
        pass

    def get_items(self):
        if not self.items:
            with open(self.csv_file, "r", encoding="utf-8") as f:
                self.items = list(csv.DictReader(f, delimiter=","))
                self._update_information(self.items)
        return self.items

    def _update_information(self, items):
        """
        update items without 'trakt_id'
        1. get trakt id according to its 'imdb_id'
        2. get media type, cause there is no show/episode for douban, only 'movie' or 'season' here.
        2. for 'season', get trakt id of the show, the season number and also the episode ids
        3. for multiple search results, fill info with the first, and record all results in 'candidates'
        """
        # init/clear props
        for item in filter(lambda x: not LocalItem.validate_id(x), items):
            LocalItem.reset_trakt_info(item)

        to_update = list(filter(lambda x: x.get("imdb_id") and not x.get("trakt_id"), items))
        if to_update:
            logger.info("information: start to update information for {} items...".format(len(to_update)))
            failures = []
            success_count = 0

            for index, item in enumerate(to_update):
                logger.debug("  [{}/{}]  Update information for {}".format(index + 1, len(to_update), LocalItem.to_string(item)))
                media, candidates = self.trakt.search_movie_or_season_by_id(item["imdb_id"], "imdb")
                # media is 'Movie' or 'Season'
                if media:
                    item["trakt_id"] = TraktItem.get_trakt_id(media)
                    item["media_type"] = TraktItem.type_name(media)
                    if TraktItem.type_name(media) == "season":
                        item["trakt_show_id"] = TraktItem.get_trakt_id(media.show)
                        item["season_number"] = media.pk
                        item["trakt_episode_ids"] = ",".join([TraktItem.get_trakt_id(x) for x in media.episodes.values()])
                    if candidates:
                        item["candidates"] = ";\n".join(list(TraktItem.to_string(x) for x in candidates))

                    success_count += 1
                    logger.debug(
                        "    Get trakt success, id: {}, type: {}, link: {}".format(item["trakt_id"], item["media_type"], TraktItem.link(media))
                    )
                else:
                    failures.append(item)
                    logger.warning("    Get trakt failed, imdb link: https://trakt.tv/search/imdb/{}".format(item["imdb_id"]))

            if success_count > 0:
                with open(self.csv_file, "w", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, items[0].keys())
                    writer.writeheader()
                    writer.writerows(items)
            if failures:
                logger.warning("  Failed items: {}".format([LocalItem.to_string(item) for x in failures]))
            logger.info("information: end of update items information, success: {}, failed: {}\n".format(success_count, len(failures)))


class TraktSource:
    def __init__(self, config):
        self.timeout = (5, 120)
        self.post_page_size = 100
        self.get_page_size = 10000
        self.auth_file = WorkingDir.get(".trakt_auth")

        self.client_id = config["client_id"]
        self.client_secret = config["client_secret"]
        self.redirect_uri = config["redirect_uri"]

        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Trakt importer",
            "Connection": "Keep-Alive",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Authorization": "",  # 'Bearer access_token',
        }

        self.username = None

        self.inited = False

    def get_watchlist(self):
        medias = Trakt["sync/watchlist"].get(pagination=True, per_page=self.get_page_size, flat=True)
        return list(medias) if isinstance(medias, PaginationIterator) else (medias if medias else [])

    def clear_watchlist(self):
        self._clear_impl("watchlist", self.get_watchlist, Trakt["sync/watchlist"])

    def add_watchlist(self, items):
        self._add_impl("watchlist", items, LocalItem.validate_id, self.get_watchlist, LocalItem.data_id, Trakt["sync/watchlist"])

    def get_watched(self, flat_to_seasons=False):
        """
        Get watched 'movies' and 'shows'
        for 'shows', it will expand to Episodes
        """
        medias = []
        for media_type in ["movies", "shows"]:
            watched = Trakt["sync/watched"].get(media=media_type)
            self._delay_for_get()
            if watched:
                if media_type == "shows" and flat_to_seasons:
                    medias.extend(TraktItem.flat_to_seasons(watched))
                else:
                    medias.extend(watched.values())
        return medias

    def clear_watched(self):
        self._clear_impl("watched", self.get_watched, Trakt["sync/history"])

    def add_watched(self, items):
        self._add_impl("watched", items, LocalItem.validate_id_date, lambda: self.get_watched(True), LocalItem.data_id_watched, Trakt["sync/history"])

    def get_ratings(self):
        ratings = Trakt["sync/ratings"].all(pagination=True, per_page=self.get_page_size, flat=True)
        return list(ratings) if isinstance(ratings, PaginationIterator) else (ratings if ratings else [])

    def clear_ratings(self):
        self._clear_impl("ratings", self.get_ratings, Trakt["sync/ratings"])

    def add_ratings(self, items):
        self._add_impl(
            "ratings", items, LocalItem.validate_id_date_rating, lambda: self.get_ratings(), LocalItem.data_id_rating, Trakt["sync/ratings"]
        )

    def get_username(self):
        if not self.username:
            settings = Trakt["users/settings"].get()
            self.username = settings["user"]["username"]
        if not self.username:
            raise Exception("Failed to get username")
        return self.username

    def get_comments(self):
        comments = []
        page = 1
        per_page = self.get_page_size
        total_pages = 1
        while page <= total_pages:
            url = "{base_url}/users/{id}/comments/{comment_type}/{type}?page={page}&limit={limit}".format(
                base_url=Trakt.base_url, id=self.get_username(), comment_type="all", type="all", page=page, limit=per_page
            )
            response = requests.get(url, headers=self.headers, timeout=self.timeout)
            if response:
                paged = json.loads(response.text)
                if paged:
                    comments.extend(paged)

                per_page = int(response.headers.get("x-pagination-limit"))
                total_pages = int(response.headers.get("x-pagination-page-count"))
                page += 1

        return comments

    def remove_comment(self, id):
        url = f"{Trakt.base_url}/comments/{id}"
        response = requests.delete(url, headers=self.headers, timeout=self.timeout)
        self._delay_for_post()
        if response.ok and response.status_code == 204:
            return True
        else:
            logger.warning(f"    Remove comment failed, code:{response.status_code}, text:{response.text}")
            return False

    def clear_comments(self):
        logger.info(f"comments: clear comments...")
        remote_data = self.get_comments()
        logger.debug("  Get comments: {}".format(TraktItem.typed_string(remote_data)))

        if remote_data:
            succeed = 0
            failed = 0
            for index in trange(len(remote_data), dynamic_ncols=True, ascii=True, unit="comment", desc=f"{' '*50}Removing..."):
                data = remote_data[index]
                if self.remove_comment(data["comment"]["id"]):
                    succeed += 1
                else:
                    failure += 1

            logger.debug(f"  Check comments after remove {len(remote_data)}(succeed={succeed}, failed={failed}) comments...")
            remote_data = self.get_comments()
            if len(remote_data) > 0:
                logger.error("Error of clear comments, {} items remained: {}".format(len(remote_data), [TraktItem.to_string(x) for x in remote_data]))
                exit(1)
            else:
                logger.debug("  Clear success")
        logger.info(f"comments: end of clear comments\n")

    def post_comment(self, item):
        url = f"{Trakt.base_url}/comments"
        data = LocalItem.data_id_comment(item)
        response = requests.post(url, data=json.dumps(data), headers=self.headers, timeout=self.timeout)
        self._delay_for_post()
        if response.ok:
            return True
        else:
            logger.warning(
                f"    Post comment failed, code:{response.status_code}, text:{response.text}, item: {LocalItem.to_string_with_comment(item)}"
            )
            return False

    def add_comments(self, items):
        def filter_to_add(_remote_data):
            _valid, _invalid = split(items, LocalItem.validate_id_comment)
            _remote_dict = dict([(TraktItem.key(x), x) for x in _remote_data])
            _added, _to_add = split(_valid, lambda x: _remote_dict.get(LocalItem.key(x)))
            return _to_add, _added, _invalid

        logger.info(f"comments: add {LocalItem.typed_string(items)} to comments...")
        to_add, added, invalid_items = filter_to_add(self.get_comments())
        if invalid_items or added:
            if invalid_items:
                logger.warning("  {} invalid items: {}".format(len(invalid_items), [LocalItem.to_string(x) for x in invalid_items]))
            if added:
                logger.info("  Already added: {}".format(LocalItem.typed_string(added)))
        logger.info("  To add: {}".format(LocalItem.typed_string(to_add)))

        if to_add:
            succeed = 0
            failed_items = []
            for index in trange(len(to_add), ascii=True, dynamic_ncols=True, unit="comment", desc=f"{' '*50}Posting..."):
                data = to_add[index]
                if self.post_comment(data):
                    succeed += 1
                else:
                    failed_items.append(data)

            logger.debug(f"  Check comments after add {len(to_add)}(succeed={succeed}, failed={len(failed_items)}) comments...")
            remote_data = self.get_comments()
            logger.debug(f"    {TraktItem.typed_string(remote_data)}")
            to_add, _, _ = filter_to_add(remote_data)
            if to_add:
                logger.warning(f"    Not added: {LocalItem.typed_string(to_add)}")
                logger.warning("      {}".format([LocalItem.to_string_with_comment(x) for x in to_add]))
        logger.info(f"comments: end of add items to watched\n")

    def search_movie_or_season_by_id(self, item_id, id_type):
        self._check_init()

        medias = Trakt["search"].lookup(item_id, id_type)
        self._delay_for_get()

        media = medias[0] if medias else None
        if media:
            if TraktItem.type_name(media) == "episode":
                # if it is an episode, find out the season according to the episode
                show = media.show
                media = self.get_season_with_episodes(media.show.get_key("trakt"), media.pk[0])
                media.show = show
            elif TraktItem.type_name(media) == "show":
                # if it is a show, find out the fist season
                show = media
                media = self.get_season_with_episodes(media.get_key("trakt"), 1)
                media.show = show
        return media, medias[1:] if medias and len(medias) > 1 else None

    def get_season_with_episodes(self, show_id, season):
        seasons = Trakt["shows"].seasons(show_id, extended="episodes")
        self._delay_for_get()
        return next(filter(lambda x: x.pk == season, seasons))

    def _add_impl(self, name, items, validate, get_remote, item_to_data, trakt_client):
        def filter_to_add(_remote_data):
            _valid, _invalid = split(items, validate)
            _remote_dict = dict([(TraktItem.key(x), x) for x in _remote_data])
            _added, _to_add = split(_valid, lambda x: _remote_dict.get(LocalItem.key(x)))
            return _to_add, _added, _invalid

        logger.info(f"{name}: add {LocalItem.typed_string(items)} to {name}...")
        to_add, added, invalid_items = filter_to_add(get_remote())
        if invalid_items or added:
            if invalid_items:
                logger.warning("  {} invalid items: {}".format(len(invalid_items), [LocalItem.to_string(x) for x in invalid_items]))
            if added:
                logger.info("  Already added: {}".format(LocalItem.typed_string(added)))
        logger.info("  To add: {}".format(LocalItem.typed_string(to_add)))

        if to_add:
            data_list = LocalItem.segment_data(to_add, item_to_data, self.post_page_size)
            for index, data in enumerate(data_list):
                logger.debug("  [{}/{}]  Add {} for {}".format(index + 1, len(data_list), name, TraktItem.typed_string_for_grouped(data)))
                response = trakt_client.add(data)
                self._delay_for_post()
                logger.debug(f"    Response: {response}")

            logger.debug(f"  Check {name} after add...")
            remote_data = get_remote()
            logger.debug(f"    {TraktItem.typed_string(remote_data)}")
            to_add, _, _ = filter_to_add(remote_data)
            if to_add:
                logger.warning(f"    Not added: {LocalItem.typed_string(to_add)}")
                logger.warning("      {}".format([LocalItem.to_string(x) for x in to_add]))
        logger.info(f"{name}: end of add items to watched\n")

    def _clear_impl(self, name, get_remote, trakt_client):
        logger.info(f"{name}: clear {name}...")
        remote_data = get_remote()
        logger.debug("  Get {}: {}".format(name, TraktItem.typed_string(remote_data)))

        data_list = TraktItem.segment_data(remote_data, self.post_page_size)
        if data_list:
            for index, data in enumerate(data_list):
                logger.debug("  [{}/{}]  Remove {} for {}".format(index + 1, len(data_list), name, TraktItem.typed_string_for_grouped(data)))
                response = trakt_client.remove(data)
                self._delay_for_post()
                logger.debug(f"    Response: {response}")

            logger.debug(f"  Check {name} after remove...")
            remote_data = get_remote()
            if len(remote_data) > 0:
                logger.error("Error of clear {}, {} items remained: {}".format(name, len(remote_data), [TraktItem.to_string(x) for x in remote_data]))
                exit(1)
            else:
                logger.debug("  Clear success")
        logger.info(f"{name}: end of clear {name}\n")

    def _wrap_request(self, make_request, progress):
        response = make_request()
        self._delay_for_post()
        logger.debug(f"    Response: {response}")
        pass

    def _check_init(self):
        if not self.inited:
            Trakt.configuration.defaults.client(id=self.client_id, secret=self.client_secret)
            Trakt.configuration.defaults.http(timeout=self.timeout)
            Trakt.on("oauth.token_refreshed", self._update_authenticate)

            self._authenticate()
            self.inited = True

    def _authenticate(self):
        # try to read form auth_file
        if os.path.exists(self.auth_file):
            with open(self.auth_file, "r") as f:
                auth = yaml.load(f, yaml.FullLoader)
                if auth:
                    self._update_authenticate(auth, False)
                    return

        logger.info("Navigate to: {}".format(Trakt["oauth"].authorize_url(self.redirect_uri)))

        code = input("Authorization code:")
        if not code:
            logger.error("Authorization code is required!")
            exit(1)

        auth = Trakt["oauth"].token_exchange(code, self.redirect_uri)
        if not auth:
            logger.error("Error occurred during token_exchange")
            exit(1)
        self._update_authenticate(auth)

    def _update_authenticate(self, auth, save=True):
        Trakt.configuration.defaults.oauth.from_response(auth, refresh=not save)
        self.headers["Authorization"] = "Bearer " + auth["access_token"]

        if save:
            logger.info("Save authorization: {}\n {}".format(auth, json.dumps(auth)))
            with open(self.auth_file, "w") as f:
                yaml.dump(auth, f)

    @staticmethod
    def _delay_for_post():
        time.sleep(1)

    @staticmethod
    def _delay_for_get():
        time.sleep(0.5)


class Client:
    def __init__(self):
        self.config = {}
        self.config_file = WorkingDir.get("config.yaml")
        self.local_file = WorkingDir.get_output("douban.csv")

    def run(self):
        self._read_config(self.config_file)
        trakt = TraktSource(self.config)
        local = LocalSource(self.local_file, trakt)
        items = local.get_items()

        if self.config.get("clear_records"):
            trakt.clear_watchlist()
            trakt.clear_watched()
            trakt.clear_ratings()
            trakt.clear_comments()

        watchlist_items = list(filter(lambda x: x["type"] == "wish", items))
        trakt.add_watchlist(watchlist_items)

        watched_items = list(filter(lambda x: x["type"] == "collect", items))
        trakt.add_watched(watched_items)

        rating_items = list(filter(lambda x: x["rating"], items))
        trakt.add_ratings(rating_items)

        comments = list(filter(lambda x: x["comment"], items))
        trakt.add_comments(comments)

    def _read_config(self, config_file):
        if os.path.exists(config_file):
            try:
                with open(config_file, "r") as yaml_file:
                    self.config = yaml.load(yaml_file, Loader=yaml.FullLoader)["trakt"]
                if not self.config.get("client_id") or not self.config.get("client_secret") or not self.config.get("redirect_uri"):
                    logger.error(
                        f"Please config client_id, client_secret and redirect_uri in the {config_file}."
                        " Create an Trakt.tv application to have your own client_id, client_secret and "
                        " redirect_uri, https://trakt.tv/oauth/applications."
                    )
                    sys.exit(1)
                if not self.config.get("clear_records"):
                    self.config["clear_records"] = False
            except Exception as e:
                logger.error(f"Error reading configuration file {config_file} with {e}")
                sys.exit(1)
        else:
            logger.error(f"Configuration file {config_file} not exists")
            sys.exit(1)


if __name__ == "__main__":
    Client().run()
