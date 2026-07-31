import inspect
from telegram.ext import ApplicationBuilder, Application
print('build', inspect.signature(ApplicationBuilder.build))
print('run_polling', inspect.signature(Application.run_polling))
print('has_post_init', hasattr(Application, 'post_init'))
print('has_create_task', hasattr(Application, 'create_task'))
print('has_create_task_callback', hasattr(Application, '_Application__create_task'))
print('builder dir', [a for a in dir(ApplicationBuilder) if 'post' in a.lower() or 'task' in a.lower() or 'init' in a.lower()])
print('application dir', [a for a in dir(Application) if 'post' in a.lower() or 'task' in a.lower() or 'init' in a.lower()])
