#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright 2023-2023 xlfu.cc <xlfu.cc@gmail.com>. All Rights Reserved.
# License:  GNU General Public License version 3 or later; see LICENSE.txt
# Website:  https://douban.com, https://github.com/xlfu-cc/douban-to-trakt.git
#
import csv
import os
import sys
import time
import requests
import yaml
from bs4 import BeautifulSoup

from file import WorkingDir
from logger import logger

_config = {}
_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"}
_cookies = {}


def requests_get(url, params=None, **kwargs):
    time.sleep(_config["sleep_interval"])
    return requests.get(url, params=params, **kwargs)


def get_imdb_id(url, title):
    r = requests_get(url, headers=_headers, cookies=_cookies)
    soup = BeautifulSoup(r.text, "lxml")
    info_area = soup.find(id="info")
    imdb_id = None
    try:
        if info_area:
            for index in range(-1, -len(info_area.find_all("span")) + 1, -1):
                imdb_id = info_area.find_all("span")[index].next_sibling.strip()
                if imdb_id.startswith("tt"):
                    break
        else:
            logger.error(f'    Can not find imdb info for "{title}", {url}')
    except Exception as e:
        logger.error(f'    Can not find imdb info for "{title}", {url}, e: {e}')
    finally:
        return imdb_id if imdb_id and imdb_id.startswith("tt") else None


def scrape_page(user_id, collect_type, start, result):
    logger.info(f"  Scrape with start={start}...")
    url = "https://movie.douban.com/people/{}/{}?start={}&sort=time&rating=all&filter=all&mode=grid".format(user_id, collect_type, start)
    r = requests_get(url, headers=_headers)
    soup = BeautifulSoup(r.text, "lxml")
    dom_items = soup.find_all("div", {"class": "item"})

    if dom_items and len(dom_items) > 0:
        logger.debug(f"    Get {len(dom_items)} items")
        for dom_item in dom_items:
            link = dom_item.a["href"]
            douban_id = link.split("/")[-2]

            item = result.get(douban_id)
            if not item:
                item = {"douban_id": douban_id}
                result[douban_id] = item

            item["type"] = collect_type

            title = dom_item.find("li", {"class": "title"}).em.text
            item["title"] = title

            rating = dom_item.find("span", {"class": "date"}).find_previous_siblings()
            item["rating"] = int(rating[0]["class"][0][6]) if len(rating) > 0 else None

            comment = dom_item.find("span", {"class": "comment"})
            item["comment"] = comment.contents[0].strip() if comment else None

            date = dom_item.find("span", {"class": "date"})
            item["date"] = date.contents[0].strip() if date else None

            if not item.get("imdb_id"):
                item["imdb_id"] = get_imdb_id(link, title)
            logger.debug(f'    Get item "{title}" with idmb: "{item["imdb_id"]}"')
    else:
        logger.error("  Scrape with start={} failed, response: {}".format(start, r))


def get_max(user_id, collect_type):
    r = requests_get(
        "https://movie.douban.com/people/{}/{}".format(user_id, collect_type),
        headers=_headers,
    )
    soup = BeautifulSoup(r.text, "lxml")

    paginator = soup.find("div", {"class": "paginator"})
    max_page = paginator.find_all("a")[-2].get_text() if paginator else 1

    subject_sum = soup.find("span", {"class": "subject-num"})
    total_count = subject_sum.get_text().split("/")[1].strip() if subject_sum else 0

    return int(max_page), int(total_count)


def load_previous(file_name):
    """
    Load data from file_name, for multi-pass scrape, read in previous scraped movie results
    """
    if os.path.exists(file_name):
        with open(file_name, "r", encoding="utf-8") as f:
            result = dict([(x["douban_id"], x) for x in csv.DictReader(f)])

            logger.debug(
                "{} items loaded, collect: {}, wish: {}".format(
                    len(result.keys()),
                    len(list(filter(lambda x: x["type"] == "collect", result.values()))),
                    len(list(filter(lambda x: x["type"] == "wish", result.values()))),
                )
            )
            return result
    return {}


def scrape(user_id, name, file_name):
    logger.info('Scrape for "{}"...'.format(name))

    logger.debug("Load previous scraped movies from {}".format(file_name))
    data_map = load_previous(file_name)

    types = ["collect", "wish"]
    total_count = 0
    for collect_type in types:
        logger.info('Scraping "{}"...'.format(collect_type))

        max_page, count = get_max(user_id, collect_type)
        total_count += count
        logger.info('  "{}" has total {} pages, {} items'.format(collect_type, max_page, count))

        for page in range(max_page):
            try:
                scrape_page(user_id, collect_type, page * 15, data_map)
            except Exception as e:
                logger.error("Error occurred when scraping with error {}".format(e))

        typed = list(filter(lambda x: x["type"] == collect_type, data_map.values()))
        logger.info(
            'Scrape "{type}" finished, success: {success}, imdb failed: {imdb_failed}, total(actual/expect): {actual}/{expect}\n'.format(
                type=collect_type,
                success=len(list(filter(lambda x: x["imdb_id"], typed))),
                imdb_failed=len(list(filter(lambda x: not x["imdb_id"], typed))),
                actual=len(typed),
                expect=count,
            )
        )

    imdb_failed = list(filter(lambda x: not x["imdb_id"], data_map.values()))
    if imdb_failed:
        logger.warning("Imdb failed items: {}".format(list(["{} - {}".format(x["douban_id"], x["title"]) for x in imdb_failed])))
    logger.info(
        "Scape finished, success: {success}, imdb failed: {imdb_failed}, total(actual/expect): {actual}/{expect}".format(
            success=len(list(filter(lambda x: x["imdb_id"], data_map.values()))),
            imdb_failed=len(imdb_failed),
            actual=len(data_map.values()),
            expect=total_count,
        )
    )

    return sorted(
        data_map.values(),
        key=lambda x: (1 if x["imdb_id"] else 0, x["type"], x["date"], x["douban_id"]),
        reverse=True,
    )


def check_user_exist(user_id):
    r = requests_get("https://m.douban.com/people/{}/".format(user_id), headers=_headers)
    soup = BeautifulSoup(r.text, "lxml")
    if "异常请求" in soup.text or soup.title is not None and "404" in soup.text:
        logger.error(soup.text)
        name = None
    else:
        name = soup.find("div", {"class": "name"}).text.strip()

    if name is None:
        logger.error(f"Douban user id {user_id} not exists")
        sys.exit(1)

    return name


def init_config(config_file):
    with open(config_file, "r") as yaml_file:
        global _config
        _config = yaml.load(yaml_file, Loader=yaml.FullLoader)["douban"]

    if not _config.get("user_id"):
        logger.error(
            f"Please config your douban user id in the {config_file}, you can open https://www.douban.com/mine/ and get the id from redirected url."
        )
        sys.exit(1)

    if not _config.get("cookies"):
        logger.error(f"Please config your douban cookies in the {config_file}.")
        sys.exit(1)
    for cookie in _config["cookies"].split(";"):
        key, value = cookie.split("=", 1)
        _cookies[key] = value

    if not _config.get("sleep_interval"):
        _config["sleep_interval"] = 2
    return _config


def write_to_csv(file_name, items):
    with open(file_name, "w", encoding="utf-8") as f:
        writer = csv.DictWriter(f, items[0].keys())
        writer.writeheader()
        writer.writerows(items)
        logger.info(f"Data exported to {file_name}")


def main():
    config_file = WorkingDir.get("config.yaml")
    config = init_config(config_file)

    user_id = config["user_id"]
    name = check_user_exist(user_id)

    file_name = WorkingDir.get_output("douban.csv")
    result = scrape(user_id, name, file_name)
    write_to_csv(file_name, result)


if __name__ == "__main__":
    main()
