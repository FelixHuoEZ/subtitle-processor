"""Runtime service container for the Flask application."""

from dataclasses import dataclass

from .file_service import FileService
from .processing_service import ProcessingService
from .readwise_service import ReadwiseService
from .runtime_metrics_service import RuntimeMetricsService
from .subtitle_service import SubtitleService
from .transcription_service import TranscriptionService
from .translation_service import TranslationService
from .video_service import VideoService


class LazyServiceProxy:
    """Lazy fallback for route modules imported outside an app factory."""

    def __init__(self, factory):
        self._factory = factory
        self._service = None

    def resolve(self):
        if self._service is None:
            self._service = self._factory()
        return self._service

    def __getattr__(self, name):
        return getattr(self.resolve(), name)


def service_proxy(factory):
    """Create a lazy service proxy for import-time route compatibility."""
    return LazyServiceProxy(factory)


@dataclass
class AppServices:
    """Container for long-lived application service instances."""

    file_service: FileService
    runtime_metrics_service: RuntimeMetricsService
    video_service: VideoService
    transcription_service: TranscriptionService
    subtitle_service: SubtitleService
    translation_service: TranslationService
    readwise_service: ReadwiseService
    processing_service: ProcessingService


def create_services() -> AppServices:
    """Create the application service set."""
    file_service = FileService()
    runtime_metrics_service = RuntimeMetricsService(
        redis_client=file_service.redis_client,
        key_prefix=file_service.redis_key_prefix,
        upload_folder=file_service.upload_folder,
        output_folder=file_service.output_folder,
    )
    video_service = VideoService(metrics_service=runtime_metrics_service)
    transcription_service = TranscriptionService()
    subtitle_service = SubtitleService()
    translation_service = TranslationService()
    readwise_service = ReadwiseService()
    processing_service = ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=transcription_service,
        subtitle_service=subtitle_service,
        readwise_service=readwise_service,
        translation_service=translation_service,
    )

    return AppServices(
        file_service=file_service,
        runtime_metrics_service=runtime_metrics_service,
        video_service=video_service,
        transcription_service=transcription_service,
        subtitle_service=subtitle_service,
        translation_service=translation_service,
        readwise_service=readwise_service,
        processing_service=processing_service,
    )


def attach_services(app, services: AppServices) -> None:
    """Attach services to the Flask app for backwards-compatible access."""
    app.services = services
    app.file_service = services.file_service
    app.runtime_metrics_service = services.runtime_metrics_service
    app.video_service = services.video_service
    app.transcription_service = services.transcription_service
    app.subtitle_service = services.subtitle_service
    app.translation_service = services.translation_service
    app.readwise_service = services.readwise_service
    app.processing_service = services.processing_service
