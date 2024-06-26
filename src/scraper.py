
from aiohttp import web, ClientSession

from aioprometheus.collectors import REGISTRY
from aioprometheus.renderer import render

from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace.status import Status
from opentelemetry.trace import StatusCode
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace import TracerProvider

import logging, os, signal, asyncio
from importlib import import_module

from scraper_configuration import get_scrapers_configuration
from keywords import choose_keyword
from exorde_data.get_target import get_target

from aioprometheus.collectors import Counter

push_counter = Counter("push", "Number of items pushed")


def setup_tracing(module: str):
    resource = Resource(attributes={
        ResourceAttributes.SERVICE_NAME: module
    })
    trace_provider = TracerProvider(resource=resource)
    jaeger_exporter = JaegerExporter(
        agent_host_name="jaeger",
        agent_port=6831,
    )
    span_processor = BatchSpanProcessor(jaeger_exporter)
    trace_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(trace_provider)

async def metrics(request):
    content, http_headers = render(REGISTRY, [request.headers.get("accept")])
    return web.Response(body=content, headers=http_headers)

async def healthcheck(request):
    return web.Response(status=200)

def terminate(signal):
    logging.info("Bye !")
    os._exit(0)

SHUTDOWN = False


async def get_parameters(module, scrap_module_name):
    scraper_configuration = await get_scrapers_configuration()
    choosen_module_path = f"https://github.com/exorde-labs/{scrap_module_name}"
    generic_modules_parameters = scraper_configuration.generic_modules_parameters
    specific_parameters = scraper_configuration.specific_modules_parameters.get(
        choosen_module_path, {}
    )
    keyword = await choose_keyword(
        scrap_module_name, scraper_configuration
    )
    parameters = {
        "url_parameters": {"keyword": keyword},
        "keyword": keyword,
    }
    parameters.update(generic_modules_parameters)
    parameters.update(specific_parameters)
    return parameters

async def get_generator(app):
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("get_generator") as get_generator_span:
        try:
            module = app["scraper_module"]
            module_name = app["module_name"]
            logging.info(f"module name is {app['module_name']}")
            parameters = await get_parameters(module, module_name)
            logging.info(f"Parameters are : {parameters}")
            generator = iterator = module.query(parameters).__aiter__()
            get_generator_span.set_status(StatusCode.OK)
        except Exception as e:
            get_generator_span.record_exception(e)
            logging.exception(
                "A critical error occured while instantiating a scraping module"
            )
            os._exit(-1)
    return generator

async def push_item(url, item):
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("push_item") as push_item_span:
        async with ClientSession() as session:
            try:
                logging.info('Pushing item')
                await session.post(url, json=item)
                push_item_span.set_status(StatusCode.OK)
            except Exception as e:
                push_item_span.set_status(Status(StatusCode.ERROR))
                logging.exception("An error occured while pushing an item")
                push_item_span.record_exception(e)

def log_environment():
    logging.info("Environment Variables:")
    for key, value in os.environ.items():
        logging.info(f"{key}: {value}")

async def scraping_task(app):
    tracer = trace.get_tracer(__name__)
    generator = None
    while not SHUTDOWN:
        logging.info("scraping loop")
        if not generator:
            generator = await get_generator(app)

        item = None

        with tracer.start_as_current_span("wait") as wait:
            try:
                item = await asyncio.wait_for(generator.__anext__(), timeout=20)
                wait.set_status(StatusCode.OK)
            except asyncio.exceptions.TimeoutError:
                logging.info("Iteration Timed-Out")
                wait.set_attribute("timeout_iteration", "true")
                wait.set_status(StatusCode.OK)
                generator = None
                item = None
            except StopAsyncIteration:
                logging.info("Stopped Iteration")
                wait.set_attribute("stop_async_iteration", "true")
                wait.set_status(StatusCode.OK)
                generator = None
                item = None
            except Exception as e:
                wait.set_status(StatusCode.ERROR, "An error occured while iterating")
                wait.record_exception(e)
                item = None
                logging.exception("An error occured while iterating")
        if item:
            target = await get_target("upipe", "UPIPE_ADDR")
            logging.info(f"pushing target is {target}")
            if target:
                await push_item(target, item)
            else:
                logging.info("Found no valid target to push to, skipping item")
                logging.info(item)
            push_counter.inc({"module": app["module_name"]})
        await asyncio.sleep(int(os.getenv("SLEEP_BETWEEN_ITEMS", 1)))

async def start_scraping_task(app):
    global SHUTDOWN
    SHUTDOWN = False
    asyncio.create_task(scraping_task(app))

app = web.Application()
app.router.add_get("/", healthcheck)
app.router.add_get("/metrics", metrics)
app.on_startup.append(start_scraping_task)

def start_scraper():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    scraper_module_name = os.environ.get("scraper_module", None)
    assert scraper_module_name
    if os.getenv("TRACE", False):
        setup_tracing(scraper_module_name)
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("service_init") as init_span:
        port = int(os.environ.get("PORT", "8000"))
        try:
            logging.info(f"Hello World ! I'm {scraper_module_name} running on {port}")
            logging.info(f"Will push data to: {os.environ.get('spotting_target')}")
            signal.signal(signal.SIGINT, terminate)
            signal.signal(signal.SIGTERM, terminate)

            scraper_module = import_module(scraper_module_name)
            app["scraper_module"] = scraper_module
            app["module_name"] = scraper_module_name

            init_span.set_status(StatusCode.OK)
        except Exception as e:
            init_span.set_status(StatusCode.ERROR, "Failed to start the scraper")
            init_span.record_exception(e)
            logging.exception("An error occured while instanciating the scraper")
    logging.info(f"Starting server on {port}")
    try:
        asyncio.run(web._run_app(app, port=port, handle_signals=True))
    except KeyboardInterrupt:
        os._exit(0)

if __name__ == '__main__':
    start_scraper()
