from flask import Flask, render_template, request, jsonify, abort
import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import logging
from logging.handlers import RotatingFileHandler
import time
import random
import json
from urllib.parse import urlparse, quote_plus
import redis
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import hashlib
import re
import threading

app = Flask(__name__)

# Enhanced logging configuration
handler = RotatingFileHandler(
    'search_engine.log',
    maxBytes=10000000,  # 10MB
    backupCount=5
)
handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
))
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

# Initialize Redis for caching
try:
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
except:
    app.logger.warning("Redis not available, falling back to in-memory cache")
    redis_client = None

class SearchResult:
    def __init__(self, title, url, snippet, category='general', date=None, favicon=None):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.category = category
        self.date = date
        self.favicon = favicon or f"https://www.google.com/s2/favicons?domain={url}"
        self.score = 0

    def to_dict(self):
        return {
            'title': self.title,
            'url': self.url,
            'display_url': self.url[:60] + '...' if len(self.url) > 60 else self.url,
            'snippet': self.snippet,
            'category': self.category,
            'date': self.date,
            'favicon': self.favicon,
            'score': self.score,
            'type': 'regular'
        }

class ImprovedSearch:
    def __init__(self):
        self.session = requests.Session()
        self.user_agent = UserAgent(fallback='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.search_urls = [
            "https://www.google.com/search",
            "https://www.bing.com/search"  # Backup search engine
        ]
        if not redis_client:
            self.in_memory_cache = {}
            self.cache_lock = threading.Lock()

    def _get_cache_key(self, query, page):
        """Generate unique cache key for query"""
        return hashlib.md5(f"{query}_{page}".encode()).hexdigest()

    def _get_from_cache(self, key):
        """Retrieve results from cache"""
        if redis_client:
            cached = redis_client.get(key)
            if cached:
                return json.loads(cached)
        else:
            with self.cache_lock:
                entry = self.in_memory_cache.get(key)
                if entry:
                    data, expire_time = entry
                    if time.time() < expire_time:
                        return data
                    else:
                        del self.in_memory_cache[key]
        return None

    def _save_to_cache(self, key, data, expire_time=3600):
        """Save results to cache"""
        if redis_client:
            redis_client.setex(key, expire_time, json.dumps(data))
        else:
            with self.cache_lock:
                self.in_memory_cache[key] = (data, time.time() + expire_time)

    def _get_headers(self):
        """Generate random headers for requests"""
        return {
            'User-Agent': self.user_agent.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
        }

    def _fetch_with_retry(self, url, params, max_retries=2, backoff_factor=0.3):
        """Enhanced fetch with exponential backoff"""
        last_exception = None

        for attempt in range(max_retries):
            try:
                # Add jitter to avoid detection
                delay = (backoff_factor * (2 ** attempt)) + random.uniform(0.1, 0.3)
                time.sleep(delay)

                response = self.session.get(
                    url,
                    params=params,
                    headers=self._get_headers(),
                    timeout=5,
                    allow_redirects=True
                )

                if response.status_code == 200:
                    return response
                elif response.status_code in [429, 403]:
                    app.logger.warning(f"Rate limited on attempt {attempt + 1} for {url}")
                    time.sleep(delay * 2)  # Additional delay for rate limits
                else:
                    app.logger.error(f"HTTP {response.status_code} on attempt {attempt + 1} for {url}")

            except requests.exceptions.RequestException as e:
                last_exception = e
                app.logger.error(f"Request failed on attempt {attempt + 1} for {url}: {str(e)}")

        if last_exception:
            raise last_exception
        else:
            raise Exception(f"Failed to fetch {url} after {max_retries} attempts")

    def _extract_date(self, text):
        """Extract date from result snippet"""
        date_patterns = [
            r'\d{1,2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s\d{4}',
            r'\d{4}-\d{2}-\d{2}',
            r'\d{1,2}/\d{1,2}/\d{4}'
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    return datetime.strptime(match.group(), '%Y-%m-%d').strftime('%b %d, %Y')
                except:
                    return match.group()
        return None

    def _categorize_result(self, url, title, snippet):
        """Enhanced result categorization"""
        domain = urlparse(url).netloc.lower()
        text = f"{title.lower()} {snippet.lower()}"

        categories = {
            'news': ['news', 'breaking', 'latest', 'report', 'update'],
            'shopping': ['shop', 'buy', 'price', 'deal', 'amazon', 'store'],
            'social': ['facebook', 'twitter', 'instagram', 'linkedin', 'reddit'],
            'video': ['youtube', 'video', 'watch', 'stream', 'vimeo'],
            'academic': ['research', 'study', 'paper', 'journal', '.edu'],
            'official': ['official', 'gov', 'organization', '.gov', '.org'],
            'tech': ['technology', 'software', 'hardware', 'review', 'digital']
        }

        for category, keywords in categories.items():
            if any(keyword in domain for keyword in keywords) or \
               any(keyword in text for keyword in keywords):
                return category

        return 'general'

    def _parse_results(self, html):
        """Enhanced result parsing with better error handling"""
        results = []
        try:
            soup = BeautifulSoup(html, 'html.parser')

            # Parse Google-style results
            for div in soup.find_all(['div', 'article'], {'class': ['g', 'result']}):
                try:
                    # Extract title
                    title_elem = div.find(['h3', 'h2', 'h1'])
                    if not title_elem:
                        continue
                    title = title_elem.get_text(strip=True)

                    # Extract URL
                    link = div.find('a')
                    if not link or not link.get('href'):
                        continue
                    url = link['href']
                    if url.startswith('/url?q='):
                        url = url.split('/url?q=')[1].split('&')[0]

                    # Extract snippet
                    snippet_elem = div.find(['div', 'span'], {'class': ['VwiC3b', 'snippet', 'description']})
                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ''

                    # Create result object
                    if title and url and snippet:
                        date = self._extract_date(snippet)
                        category = self._categorize_result(url, title, snippet)
                        result = SearchResult(title, url, snippet, category, date)
                        results.append(result)

                except Exception as e:
                    app.logger.error(f"Error parsing individual result: {str(e)}")
                    continue

        except Exception as e:
            app.logger.error(f"Error parsing HTML: {str(e)}")

        return results

    def _rank_results(self, query, results):
        """Enhanced result ranking"""
        query_terms = query.lower().split()

        for result in results:
            score = 0

            # Title matching
            title_lower = result.title.lower()
            if query.lower() in title_lower:
                score += 30  # Exact query match in title
            score += sum(10 for term in query_terms if term in title_lower)

            # URL quality
            domain = urlparse(result.url).netloc
            if any(tld in domain for tld in ['.edu', '.gov', '.org']):
                score += 15  # Trusted domains
            if len(domain.split('.')) == 2:  # Prefer root domains
                score += 5

            # Content relevance
            snippet_lower = result.snippet.lower()
            score += sum(5 for term in query_terms if term in snippet_lower)

            # Freshness
            if result.date:
                try:
                    date = datetime.strptime(result.date, '%b %d, %Y')
                    days_old = (datetime.now() - date).days
                    if days_old < 30:
                        score += 20  # Very recent
                    elif days_old < 90:
                        score += 10  # Fairly recent
                except:
                    pass

            # Category relevance
            if result.category in ['news', 'official', 'academic']:
                score += 10

            result.score = score

        return sorted(results, key=lambda x: x.score, reverse=True)

    def _search_single_engine(self, search_url, query, page):
        try:
            params = {
                'q': query,
                'start': (page - 1) * 10,
                'num': 10,
                'hl': 'en',
                'safe': 'active'
            }
            response = self._fetch_with_retry(search_url, params)
            if response and response.text:
                current_results = self._parse_results(response.text)
                return current_results
        except Exception as e:
            app.logger.error(f"Search error on {search_url}: {str(e)}")
            return []
        return []

    def search(self, query, page=1):
        """Main search method with fallback and error handling"""
        cache_key = self._get_cache_key(query, page)
        cached_results = self._get_from_cache(cache_key)

        if cached_results:
            return cached_results

        results = []
        errors = []

        # Submit tasks to the executor
        futures = []
        for search_url in self.search_urls:
            future = self.executor.submit(self._search_single_engine, search_url, query, page)
            futures.append(future)

        # Collect results as they complete
        for future in as_completed(futures):
            try:
                current_results = future.result()
                results.extend(current_results)
                if len(results) >= 5:  # We have enough results
                    break
            except Exception as e:
                errors.append(str(e))
                continue

        if not results and errors:
            app.logger.error("\n".join(errors))
            return []

        ranked_results = self._rank_results(query, results)
        serialized_results = [result.to_dict() for result in ranked_results]

        # Cache the results
        self._save_to_cache(cache_key, serialized_results)

        return serialized_results

    def get_suggestions(self, query):
        """Get search suggestions with error handling"""
        if not query or len(query) < 2:
            return []

        cache_key = f"suggest_{query}"
        cached_suggestions = self._get_from_cache(cache_key)

        if cached_suggestions:
            return cached_suggestions

        try:
            params = {
                'client': 'chrome',
                'q': query
            }
            response = self._fetch_with_retry(
                'https://suggestqueries.google.com/complete/search',
                params
            )

            if response and response.status_code == 200:
                suggestions = json.loads(response.text)[1]
                self._save_to_cache(cache_key, suggestions, expire_time=1800)
                return suggestions

        except Exception as e:
            app.logger.error(f"Suggestion error: {str(e)}")

        return []

# Initialize search engine
search_engine = ImprovedSearch()

@app.route('/')
def home():
    return render_template('search.html')

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    page = max(1, int(request.args.get('page', 1)))

    if not query:
        return render_template('search.html')

    try:
        results = search_engine.search(query, page)

        # Group results by category
        categorized_results = {}
        for result in results:
            if result['type'] == 'info_box':
                continue
            category = result['category']
            if category not in categorized_results:
                categorized_results[category] = []
            categorized_results[category].append(result)

        return render_template(
            'search.html',
            query=query,
            results=results,
            categorized_results=categorized_results,
            page=page,
            total_results=len(results)
        )

    except Exception as e:
        app.logger.error(f"Search route error: {str(e)}")
        return render_template(
            'search.html',
            query=query,
            error="An error occurred while processing your search. Please try again."
        )

@app.route('/suggest')
def suggest():
    query = request.args.get('q', '').strip()
    try:
        suggestions = search_engine.get_suggestions(query)
        return jsonify(suggestions)
    except Exception as e:
        app.logger.error(f"Suggestion route error: {str(e)}")
        return jsonify([])

@app.errorhandler(404)
def not_found_error(error):
    return render_template('search.html', error="Page not found"), 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f"Internal server error: {str(error)}")
    return render_template('search.html', error="An internal error occurred. Please try again."), 500

if __name__ == "__main__":
    app.run(debug=False, host='0.0.0.0', port=5000)
